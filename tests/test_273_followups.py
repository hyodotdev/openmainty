"""Gate (2) Slice 1 (273): stable-surface observer methods + canary admission.

Offline only: model/list + usage/read parsers and thin query wrappers,
canary_admission verdict (single-use, never full admission), the
preflight observer `controls` seam (default honest-unknown), and the
MUSE_CONTRIBUTOR_MODE ambient gate. No spawn, no quota, no native code.

Production behavior must not change: config stays unestablished,
credential_source stays unknown, native_containment still raises, and
a live preflight for an unchanged world still refuses
config-unestablished (pinned by ProductionUnchangedCase).

Isolated temp homes only; never the real home. No deletes anywhere:
TemporaryDirectory cleanup only.
"""

import ast
import functools
import hashlib
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
from openmainty import preflight as pf  # noqa: E402
from openmainty.preflight import PreflightError  # noqa: E402
from test_preflight import (  # noqa: E402
    _MspScriptedChild, _MSP_WORKER_SID_A, _fake_transport,
    _fixture_observer, _fixture_values, _msp_test_init_result,
    _msp_test_line, _msp_test_ok, _msp_worker_start_result)

SENTINEL = "SENTINEL273-DO-NOT-RECORD"


def _model_list_result(rows=None, **over):
    if rows is None:
        rows = [
            {"modelId": "muse-spark-1.3", "providerId": "meta",
             "profileId": None, "isActive": True, "isDefault": True,
             "displayLabel": "DROP-ME", "description": "DROP-ME",
             "cost": {"perCall": 1}, "contextLimit": 1000,
             "outputLimit": 100, "releaseDate": None,
             "variants": {"efforts": ["high"]},
             "reasoningEffortVariants": [],
             "defaultReasoningEffort": "high"},
            {"modelId": "other-model", "providerId": "meta",
             "profileId": None, "isActive": False, "isDefault": False,
             "displayLabel": "DROP-ME", "description": "DROP-ME",
             "cost": None, "contextLimit": None, "outputLimit": None,
             "releaseDate": None},
        ]
    doc = {"models": rows, "profileId": None, "providerId": "meta",
           "source": "providerCatalog"}
    doc.update(over)
    return doc


def _usage_result(**over):
    doc = {"usage": {
        "observedAtMs": 1730000000000, "tier": "pro-monthly",
        "window": {"resetsAtMs": 1730010000000, "usedPercent": 12,
                   "windowDurationMins": 300},
        "weekly": {"resetsAtMs": 1730600000000, "usedPercent": 3},
    }}
    doc.update(over)
    return doc


def _msp_test_err(ident, code=-32601):
    return _msp_test_line(
        {"jsonrpc": "2.0", "id": ident,
         "error": {"code": code, "message": "method not found"}})


class ModelListParseCase(unittest.TestCase):
    def test_golden_extracts_active_and_drops_labels(self):
        out = muse_mod.parse_msp_model_list(
            _model_list_result(), _MSP_WORKER_SID_A)
        self.assertEqual(out["provider_id"], "meta")
        self.assertIsNone(out["profile_id"])
        self.assertEqual(out["source"], "providerCatalog")
        self.assertTrue(out["source_live"])
        self.assertEqual(out["rows"], 2)
        self.assertEqual(out["active"],
                         {"model_id": "muse-spark-1.3",
                          "provider_id": "meta"})
        self.assertEqual(out["for_session"], _MSP_WORKER_SID_A)
        blob = json.dumps(out, sort_keys=True)
        self.assertNotIn("DROP-ME", blob)
        self.assertNotIn("displayLabel", blob)
        self.assertNotIn("cost", blob)

    def test_no_active_row_is_unknown_not_error(self):
        rows = [
            {"modelId": "muse-spark-1.3", "providerId": "meta",
             "profileId": None, "isActive": False, "isDefault": True},
        ]
        out = muse_mod.parse_msp_model_list(
            _model_list_result(rows), _MSP_WORKER_SID_A)
        self.assertIsNone(out["active"])
        self.assertEqual(out["rows"], 1)

    def test_non_meta_provider_refuses(self):
        with self.assertRaises(muse_mod.MspError) as cm:
            muse_mod.parse_msp_model_list(
                _model_list_result(providerId="other"),
                _MSP_WORKER_SID_A)
        self.assertEqual(cm.exception.code, "provider-mismatch")

    def test_fake_source_parses_with_live_false(self):
        out = muse_mod.parse_msp_model_list(
            _model_list_result(source="fakeCatalog"),
            _MSP_WORKER_SID_A)
        self.assertEqual(out["source"], "fakeCatalog")
        self.assertFalse(out["source_live"])
        self.assertIsNotNone(out["active"])

    def test_malformed_shapes_refuse(self):
        good_rows = _model_list_result()["models"]
        cases = [
            ("non-dict", "not-a-dict"),
            ("missing-models",
             {"profileId": None, "providerId": "meta",
              "source": "providerCatalog"}),
            ("models-not-list",
             _model_list_result(rows={"no": "list"})),
            ("row-65",
             _model_list_result(
                 rows=[dict(good_rows[1]) for _ in range(65)])),
            ("two-active",
             _model_list_result(rows=[
                 dict(good_rows[0], isActive=True),
                 dict(good_rows[1], isActive=True)])),
            ("row-missing-model",
             _model_list_result(rows=[
                 {"providerId": "meta", "isActive": False}])),
            ("row-empty-model",
             _model_list_result(rows=[
                 {"modelId": "", "providerId": "meta",
                  "isActive": False}])),
            ("row-bad-active",
             _model_list_result(rows=[
                 {"modelId": "m", "providerId": "meta",
                  "isActive": "yes"}])),
            ("row-non-dict",
             _model_list_result(rows=["muse-spark-1.3"])),
            ("missing-source",
             {"models": good_rows, "profileId": None,
              "providerId": "meta"}),
        ]
        for name, doc in cases:
            with self.subTest(name=name):
                with self.assertRaises(muse_mod.MspError) as cm:
                    muse_mod.parse_msp_model_list(doc,
                                                  _MSP_WORKER_SID_A)
                self.assertEqual(cm.exception.code, "malformed",
                                 name)

    def test_bad_session_id_refuses(self):
        with self.assertRaises(muse_mod.MspError):
            muse_mod.parse_msp_model_list(_model_list_result(),
                                          "not-a-uuid")


class UsageParseCase(unittest.TestCase):
    def test_golden_round_trips_verbatim(self):
        out = muse_mod.parse_msp_usage(_usage_result())
        self.assertEqual(out, {
            "observed": True, "observed_at_ms": 1730000000000,
            "tier": "pro-monthly",
            "window": {"resetsAtMs": 1730010000000,
                       "usedPercent": 12,
                       "windowDurationMins": 300},
            "weekly": {"resetsAtMs": 1730600000000,
                       "usedPercent": 3},
        })

    def test_omitted_usage_is_unknown_not_error(self):
        self.assertEqual(muse_mod.parse_msp_usage({}),
                         {"observed": False})
        self.assertEqual(
            muse_mod.parse_msp_usage({"unrelated": 1}),
            {"observed": False})

    def test_over_quota_percent_is_valid(self):
        doc = _usage_result()
        doc["usage"]["window"]["usedPercent"] = 140
        out = muse_mod.parse_msp_usage(doc)
        self.assertEqual(out["window"]["usedPercent"], 140)

    def test_malformed_shapes_refuse(self):
        good = _usage_result()["usage"]
        cases = [
            ("non-dict", "not-a-dict"),
            ("usage-non-dict", {"usage": [1]}),
            ("bool-stamp",
             {"usage": dict(good, observedAtMs=True)}),
            ("negative-stamp",
             {"usage": dict(good, observedAtMs=-1)}),
            ("empty-tier",
             {"usage": dict(good, tier="")}),
            ("long-tier",
             {"usage": dict(good, tier="t" * 129)}),
            ("window-non-dict",
             {"usage": dict(good, window=[1])}),
            ("negative-percent",
             {"usage": dict(
                 good, window=dict(good["window"],
                                   usedPercent=-1))}),
            ("zero-duration",
             {"usage": dict(
                 good, window=dict(good["window"],
                                   windowDurationMins=0))}),
            ("weekly-missing",
             {"usage": {k: v for k, v in good.items()
                        if k != "weekly"}}),
        ]
        for name, doc in cases:
            with self.subTest(name=name):
                with self.assertRaises(muse_mod.MspError) as cm:
                    muse_mod.parse_msp_usage(doc)
                self.assertEqual(cm.exception.code, "malformed",
                                 name)

    def test_staleness_decided_at_stage_b_not_parse(self):
        old = _usage_result()
        old["usage"]["observedAtMs"] = 1000
        out = muse_mod.parse_msp_usage(old)
        self.assertEqual(out["observed_at_ms"], 1000)
        self.assertFalse(
            muse_mod.msp_usage_fresh_after(out, 1000))
        self.assertFalse(
            muse_mod.msp_usage_fresh_after(out, 2000))
        self.assertTrue(
            muse_mod.msp_usage_fresh_after(out, 999))
        self.assertFalse(muse_mod.msp_usage_fresh_after(
            {"observed": False}, 0))


class Worker273Mixin:
    """Scripted-child worker fixture (TemporaryDirectory only).

    Mirrors MspWorkerSessionCase's seam discipline (scripted fake
    child on real pipes, strict autospec seams) but every directory
    is a TemporaryDirectory: no mkdtemp/rmtree anywhere. Handles
    close explicitly per test (never addCleanup-close: closing an
    already-closed handle would raise in cleanup).
    """

    def _scrub_env(self):
        for var in ("META_API_KEY", "MUSE_CONTRIBUTOR_MODE",
                    "OPENMAINTY_MUSE_BINARY"):
            prev = os.environ.pop(var, None)
            if prev is not None:
                self.addCleanup(os.environ.__setitem__, var, prev)

    def _worker_fixture(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="msp273-")
        self.addCleanup(self.tmp.cleanup)
        self.work = os.path.realpath(
            os.path.join(self.tmp.name, "work"))
        os.makedirs(self.work, mode=0o700)
        self._scrub_env()
        self.bin_path = os.path.join(self.work, "muse-bin")
        with open(self.bin_path, "wb") as fh:
            fh.write(b"owned-fixture-binary-273")
        os.chmod(self.bin_path, 0o700)
        bst = os.stat(self.bin_path)
        self.snap = {"path": self.bin_path,
                     "real_path": self.bin_path,
                     "sha256": hashlib.sha256(
                         b"owned-fixture-binary-273").hexdigest(),
                     "dev": bst.st_dev, "ino": bst.st_ino,
                     "size": bst.st_size}
        self.policy_path = os.path.join(self.work, "POLICY.sb")
        with open(self.policy_path, "wb") as fh:
            fh.write(b"fixture-policy-273")
        pst = os.stat(self.policy_path)
        self.policy = {"path": self.policy_path,
                       "sha256": hashlib.sha256(
                           b"fixture-policy-273").hexdigest(),
                       "size": pst.st_size, "dev": pst.st_dev,
                       "ino": pst.st_ino}
        for name, value in (
                ("_msp_resolve_snapshot", dict(self.snap)),
                ("_msp_build_wrapper",
                 {"argv": ["sandbox-exec", "-f", "POLICY", "EXE"],
                  "policy": dict(self.policy)})):
            patcher = mock.patch.object(muse_mod, name,
                                        return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.spawned = []
        self._worker_patchers = []
        self.addCleanup(self._stop_worker_patches)

    def _stop_worker_patches(self):
        for patcher in reversed(self._worker_patchers):
            patcher.stop()
        self._worker_patchers = []

    def _spawn_with(self, child):
        def _spawn(cmd, env, cwd):
            self.spawned.append((cmd, env, cwd))
            return child

        patcher = mock.patch.object(
            muse_mod, "_msp_spawn_process", autospec=True,
            side_effect=_spawn)
        patcher.start()
        self._worker_patchers.append(patcher)

    def _methods(self, child):
        return [json.loads(line.decode("utf-8"))
                for line in child.raw_requests]

    def _bootstrap(self, account_state="loggedOut"):
        self._stop_worker_patches()
        root_tmp = tempfile.TemporaryDirectory(
            prefix="msp273-root-")
        self.addCleanup(root_tmp.cleanup)
        self.root = os.path.realpath(root_tmp.name)
        os.chmod(self.root, 0o700)
        home = os.path.join(self.root, "data", "muse")
        child = _MspScriptedChild(script={
            "initialize": [_msp_test_ok(
                1, _msp_test_init_result(home))],
            "account/read": [_msp_test_ok(
                2, {"state": account_state,
                    "credentialRequired": True})],
        })
        self.addCleanup(child.force_close)
        self._spawn_with(child)
        handle = muse_mod.spawn_msp_worker(self.root)
        muse_mod.msp_worker_bootstrap(
            handle, timeout=30, app_version="t")
        return handle, child

    def _start_session(self, handle, child, req_id,
                       name="ws"):
        ws = os.path.join(self.root, name)
        os.mkdir(ws, 0o700)
        child._script["session/start"] = [_msp_test_ok(
            req_id, _msp_worker_start_result(
                _MSP_WORKER_SID_A, ws))]
        bound = muse_mod.msp_worker_session_start(
            handle, workspace_root=ws, timeout=10)
        self.assertEqual(bound["id"], _MSP_WORKER_SID_A)
        return ws

    def _close(self, handle):
        out = muse_mod.msp_worker_close(handle, reason="t")
        self.assertEqual(out["state"], "clean")
        self.assertNotIn(handle, muse_mod._MSP_WORKERS)


class WorkerQueryCase(Worker273Mixin, unittest.TestCase):
    def setUp(self):
        self._worker_fixture()

    def test_model_list_sends_bound_session_id(self):
        handle, child = self._bootstrap("accountLogin")
        try:
            self._start_session(handle, child, 3)
            child._script["model/list"] = [_msp_test_ok(
                4, _model_list_result())]
            out = muse_mod.msp_worker_model_list(
                handle, timeout=10)
            self.assertEqual(
                out["active"]["model_id"], "muse-spark-1.3")
            self.assertTrue(out["source_live"])
            sent = [obj for obj in self._methods(child)
                    if obj.get("method") == "model/list"]
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0]["params"],
                             {"sessionId": _MSP_WORKER_SID_A})
        finally:
            self._close(handle)

    def test_usage_sends_empty_params(self):
        handle, child = self._bootstrap("accountLogin")
        try:
            child._script["usage/read"] = [_msp_test_ok(
                3, _usage_result())]
            out = muse_mod.msp_worker_usage(handle, timeout=10)
            self.assertTrue(out["observed"])
            self.assertEqual(out["tier"], "pro-monthly")
            sent = [obj for obj in self._methods(child)
                    if obj.get("method") == "usage/read"]
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0]["params"], {})
        finally:
            self._close(handle)

    def test_model_list_needs_bound_session(self):
        handle, _child = self._bootstrap("accountLogin")
        try:
            with self.assertRaises(muse_mod.MspError) as cm:
                muse_mod.msp_worker_model_list(
                    handle, timeout=10)
            self.assertEqual(cm.exception.code, "no-session")
        finally:
            self._close(handle)

    def test_unknown_method_refuses_without_retry(self):
        handle, child = self._bootstrap("accountLogin")
        try:
            self._start_session(handle, child, 3)
            child._script["model/list"] = [_msp_test_err(4)]
            with self.assertRaises(muse_mod.MspError) as cm:
                muse_mod.msp_worker_model_list(
                    handle, timeout=10)
            self.assertEqual(cm.exception.code, "method-error")
            sent = [obj for obj in self._methods(child)
                    if obj.get("method") == "model/list"]
            self.assertEqual(len(sent), 1)
        finally:
            self._close(handle)
        handle2, child2 = self._bootstrap("accountLogin")
        try:
            child2._script["usage/read"] = [_msp_test_err(3)]
            with self.assertRaises(muse_mod.MspError) as cm2:
                muse_mod.msp_worker_usage(handle2, timeout=10)
            self.assertEqual(cm2.exception.code, "method-error")
            sent2 = [obj for obj in self._methods(child2)
                     if obj.get("method") == "usage/read"]
            self.assertEqual(len(sent2), 1)
        finally:
            self._close(handle2)

    def test_worker_methods_admit_new_queries(self):
        self.assertIn("model/list", muse_mod._MSP_WORKER_METHODS)
        self.assertIn("usage/read", muse_mod._MSP_WORKER_METHODS)


class CanaryAdmissionCase(Worker273Mixin, unittest.TestCase):
    def setUp(self):
        self._worker_fixture()

    def _admit_handle(self, lane_state, usage, hooks):
        handle, _child = self._bootstrap(lane_state)
        ws = os.path.join(self.root, "ws")
        os.mkdir(ws, 0o700)
        rec = muse_mod._MSP_WORKERS[handle]
        rec["worker_session"] = {
            "id": _MSP_WORKER_SID_A, "status": "idle",
            "workspace_root": ws, "workspace_dev": 1,
            "workspace_ino": 1}
        rec["config_established"] = True
        rec["worker_hook_count"] = hooks
        if usage is not None:
            rec["worker_usage"] = usage
        return handle

    def test_matrix_lane_usage_hooks(self):
        fresh_usage = muse_mod.parse_msp_usage(_usage_result())
        rows = [
            ("candidate", "accountLogin", None, 0, True),
            ("candidate", "accountLogin", None, 2, False),
            ("candidate", "accountLogin", fresh_usage, 0,
             False),
            ("candidate", "accountLogin", fresh_usage, 2,
             False),
            ("other", "loggedOut", None, 0, False),
            ("other", "loggedOut", None, 2, False),
            ("other", "loggedOut", fresh_usage, 0, False),
            ("other", "loggedOut", fresh_usage, 2, False),
        ]
        for lane, state, usage, hooks, want in rows:
            name = "%s-%s-hooks%d" % (
                lane, "usage" if usage else "nousage", hooks)
            with self.subTest(name=name):
                handle = self._admit_handle(
                    state, usage, hooks)
                try:
                    out = muse_mod.canary_admission(handle)
                    self.assertEqual(
                        out["admitted_once"], want, name)
                    if want:
                        self.assertEqual(out["missing"], [],
                                         name)
                    else:
                        self.assertTrue(out["missing"], name)
                    cap = muse_mod.msp_worker_capability(handle)
                    self.assertFalse(cap["admitted"], name)
                finally:
                    self._close(handle)

    def test_single_use_consumes(self):
        handle = self._admit_handle("accountLogin", None, 0)
        try:
            first = muse_mod.canary_admission(handle)
            self.assertTrue(first["admitted_once"])
            rec = muse_mod._MSP_WORKERS[handle]
            self.assertTrue(rec.get("worker_canary_consumed"))
            second = muse_mod.canary_admission(handle)
            self.assertFalse(second["admitted_once"])
            self.assertIn("canary-already-consumed",
                          second["missing"])
        finally:
            self._close(handle)

    def test_drifted_member_refuses(self):
        handle = self._admit_handle("accountLogin", None, 0)
        try:
            rec = muse_mod._MSP_WORKERS[handle]
            rec["worker_session"] = None
            out = muse_mod.canary_admission(handle)
            self.assertFalse(out["admitted_once"])
            self.assertIn("session-unbound", out["missing"])
        finally:
            self._close(handle)
        handle2 = self._admit_handle("accountLogin", None, 0)
        try:
            rec2 = muse_mod._MSP_WORKERS[handle2]
            rec2["worker_account"] = {
                "state": "loggedOut",
                "credential_required": True}
            out2 = muse_mod.canary_admission(handle2)
            self.assertFalse(out2["admitted_once"])
            self.assertIn("winning-credential", out2["missing"])
        finally:
            self._close(handle2)

    def test_unknown_and_closed_handles_refuse(self):
        with self.assertRaises(muse_mod.MspError) as cm:
            muse_mod.canary_admission("no-such-handle")
        self.assertEqual(cm.exception.code, "unknown-handle")
        handle = self._admit_handle("accountLogin", None, 0)
        self._close(handle)
        with self.assertRaises(muse_mod.MspError) as cm2:
            muse_mod.canary_admission(handle)
        self.assertEqual(cm2.exception.code, "unknown-handle")


class ObserverSeamCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pf273-")
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(self.home, mode=0o700)
        for var in ("META_API_KEY", "MUSE_CONTRIBUTOR_MODE"):
            prev = os.environ.pop(var, None)
            if prev is not None:
                self.addCleanup(os.environ.__setitem__, var,
                                prev)
        self.values = _fixture_values()
        cur = mock.patch("openmainty.preflight.current_identity",
                         return_value=self.values)
        cur.start()
        self.addCleanup(cur.stop)
        popen = mock.patch.object(
            muse_mod.subprocess, "Popen",
            side_effect=AssertionError("no spawn in offline tests"))
        popen.start()
        self.addCleanup(popen.stop)

    def _controls(self):
        channels = {}
        for name in pf.REQUIRED_CHANNELS:
            channels[name] = {
                "required": pf.REQUIRED_DIRECTION[name],
                "observed": pf.REQUIRED_DIRECTION[name],
                "positive": True,
                "detail": "controls seam: injected marker",
            }
        auth = {"meta_api_key_present": False,
                "credential_source": "owned-fixture-controls",
                "billing_evidence": None,
                "observed_how": "controls seam",
                "contributor_env_present": False}
        return {"channels": channels, "auth": auth}

    def test_controls_drive_verified_end_to_end(self):
        created = pf.create_preflight(self.home, "controls-v")
        out = pf.run_preflight(
            self.home, created["pfid"],
            transport=_fake_transport(),
            observer=functools.partial(
                pf._default_observer,
                controls=self._controls()))
        self.assertEqual(out["state"], "verified")
        self.assertEqual(out["code"], "")
        outcome, _raw = pf._load_doc(
            self.home, created["pfid"], "outcome",
            pf.PREFLIGHT_OUTCOME_MAX_BYTES)
        self.assertEqual(outcome["state"], "verified")

    def test_malformed_controls_refuse_probe_failed(self):
        for name, controls in (
                ("non-dict", "nope"),
                ("missing-auth", {"channels": {}}),
                ("missing-channels", {"auth": {}}),
                ("channels-non-dict",
                 {"channels": [], "auth": {}})):
            with self.subTest(name=name):
                created = pf.create_preflight(
                    self.home, "controls-bad-" + name)
                out = pf.run_preflight(
                    self.home, created["pfid"],
                    transport=_fake_transport(),
                    observer=functools.partial(
                        pf._default_observer, controls=controls))
                self.assertEqual(out["state"], "refused", name)
                self.assertEqual(out["code"], "probe-failed",
                                 name)

    def test_default_without_controls_byte_identical(self):
        inputs = {"auth_observed": self.values["auth"]}
        spawn = {"exit_code": 0, "output_bytes": 2,
                 "usage": {}}
        plain = pf._default_observer(
            self.home, "pf-x", inputs, spawn)
        explicit = pf._default_observer(
            self.home, "pf-x", inputs, spawn, controls=None)
        self.assertEqual(plain, explicit)
        sig = inspect.signature(pf._default_observer)
        self.assertIsNone(
            sig.parameters["controls"].default)
        channels, auth = plain
        for name in pf.REQUIRED_CHANNELS:
            self.assertEqual(channels[name]["observed"],
                             "unknown")
            self.assertFalse(channels[name]["positive"])
        self.assertEqual(auth["credential_source"], "unknown")


class ContributorEnvCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pf273-")
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(self.home, mode=0o700)
        for var in ("META_API_KEY", "MUSE_CONTRIBUTOR_MODE",
                    "OPENMAINTY_MUSE_BINARY"):
            prev = os.environ.pop(var, None)
            if prev is not None:
                self.addCleanup(os.environ.__setitem__, var,
                                prev)
        popen = mock.patch.object(
            muse_mod.subprocess, "Popen",
            side_effect=AssertionError("no spawn in offline tests"))
        popen.start()
        self.addCleanup(popen.stop)

    def test_present_value_refuses_spawn(self):
        with mock.patch.dict(os.environ,
                             {"MUSE_CONTRIBUTOR_MODE": "1"}):
            code, _out, usage = muse_mod.exec_source_repair(
                "probe", self.home, admission_kind="stage0",
                preflight_home=self.home)
        self.assertEqual(code, 125)
        self.assertEqual(usage.get("code"),
                         "contributor-env-present")

    def test_empty_value_refuses_spawn(self):
        with mock.patch.dict(os.environ,
                             {"MUSE_CONTRIBUTOR_MODE": ""}):
            code, _out, usage = muse_mod.exec_source_repair(
                "probe", self.home, admission_kind="stage0",
                preflight_home=self.home)
        self.assertEqual(code, 125)
        self.assertEqual(usage.get("code"),
                         "contributor-env-present")

    def test_unset_falls_through_to_next_gate(self):
        code, _out, usage = muse_mod.exec_source_repair(
            "probe", self.home, admission_kind="stage0",
            preflight_home=self.home)
        self.assertEqual(code, 125)
        self.assertNotEqual(usage.get("code"),
                            "contributor-env-present")

    def test_narrowed_env_key_is_internal_error(self):
        with mock.patch.object(
                muse_mod, "_dedicated_child_env",
                return_value={"PATH": "x",
                              "MUSE_CONTRIBUTOR_MODE": "1"}):
            with mock.patch(
                    "openmainty.preflight.stage0_admission",
                    return_value={}):
                with mock.patch.object(
                        muse_mod, "resolve_pinned_muse",
                        return_value={"path": "p",
                                      "real_path": "p"}):
                    code, _out, usage = \
                        muse_mod.exec_source_repair(
                            "probe", self.home,
                            admission_kind="stage0",
                            preflight_home=self.home)
        self.assertEqual(code, 125)
        self.assertEqual(usage.get("code"),
                         "contributor-env-present")
        self.assertIn("internal error", usage.get("error", ""))

    def test_worker_and_host_spawns_refuse(self):
        with mock.patch.dict(os.environ,
                             {"MUSE_CONTRIBUTOR_MODE": "0"}):
            for name, func in (
                    ("worker", muse_mod.spawn_msp_worker),
                    ("host", muse_mod.spawn_msp_host)):
                with self.subTest(name=name):
                    root = os.path.join(self.tmp.name, name)
                    os.mkdir(root, 0o700)
                    with self.assertRaises(
                            muse_mod.MspError) as cm:
                        func(os.path.realpath(root))
                    self.assertEqual(cm.exception.code,
                                     "contributor-env-present",
                                     name)


class ProductionUnchangedCase(unittest.TestCase):
    """Golden: production defaults are byte-identical after Slice 1."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pf273-")
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(self.home, mode=0o700)
        for var in ("META_API_KEY", "MUSE_CONTRIBUTOR_MODE",
                    "OPENMAINTY_MUSE_BINARY"):
            prev = os.environ.pop(var, None)
            if prev is not None:
                self.addCleanup(os.environ.__setitem__, var,
                                prev)
        popen = mock.patch.object(
            muse_mod.subprocess, "Popen",
            side_effect=AssertionError("no spawn in offline tests"))
        popen.start()
        self.addCleanup(popen.stop)

    def test_real_identity_still_unestablished_unknown(self):
        ident = pf.current_identity()
        self.assertEqual(ident["config"]["status"],
                         "unestablished")
        self.assertEqual(ident["config"]["files"], [])
        self.assertEqual(ident["auth"]["credential_source"],
                         "unknown")
        self.assertIs(
            ident["auth"]["contributor_env_present"], False)
        self.assertIs(
            ident["auth"]["meta_api_key_present"], False)
        self.assertIsNone(ident["auth"]["billing_evidence"])

    def test_default_observer_still_all_unknown(self):
        ident = pf.current_identity()
        inputs = {"auth_observed": ident["auth"]}
        spawn = {"exit_code": 125, "output_bytes": 0,
                 "usage": {}}
        channels, auth = pf._default_observer(
            self.home, "pf-golden", inputs, spawn)
        for name in pf.REQUIRED_CHANNELS:
            self.assertEqual(
                channels[name],
                {"required": pf.REQUIRED_DIRECTION[name],
                 "observed": "unknown", "positive": False,
                 "detail": channels[name]["detail"]})
            self.assertTrue(
                channels[name]["detail"].startswith(
                    "no app-observed positive control"))
        self.assertEqual(
            auth,
            {"meta_api_key_present": False,
             "credential_source": "unknown",
             "billing_evidence": None,
             "observed_how": "default observer: no supported "
                             "trusted auth-source mechanism "
                             "established",
             "contributor_env_present": False})

    def test_native_containment_still_raises(self):
        with self.assertRaises(PreflightError) as cm:
            pf.native_containment()
        self.assertEqual(cm.exception.code,
                         "native-containment-unproved")

    def test_live_shape_still_refuses_config_unestablished(self):
        created = pf.create_preflight(self.home, "golden-live")
        out = pf.run_preflight(self.home, created["pfid"])
        self.assertEqual(out["state"], "refused")
        self.assertEqual(out["code"], "config-unestablished")


class ServeSeamCase(unittest.TestCase):
    def test_no_serve_route_passes_observer_or_controls(self):
        import openmainty.serve as serve_mod
        path = serve_mod.__file__
        with open(path, "rb") as fh:
            tree = ast.parse(fh.read())
        calls = []

        class _Finder(ast.NodeVisitor):
            def visit_Call(self, node):
                func = node.func
                hit = False
                if isinstance(func, ast.Attribute):
                    hit = func.attr == "run_preflight"
                elif isinstance(func, ast.Name):
                    hit = func.id == "run_preflight"
                if hit:
                    calls.append(node)
                self.generic_visit(node)

        _Finder().visit(tree)
        self.assertTrue(calls, "expected run_preflight calls")
        for node in calls:
            kinds = {kw.arg for kw in node.keywords}
            self.assertFalse(
                kinds & {"observer", "transport", "controls"},
                ast.dump(node))
            self.assertLessEqual(len(node.args), 2,
                                 ast.dump(node))

    def test_seam_defaults_are_none(self):
        self.assertIsNone(
            inspect.signature(
                pf.run_preflight).parameters["observer"].default)
        self.assertIsNone(
            inspect.signature(
                pf._default_observer).parameters[
                    "controls"].default)


class SentinelCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pf273-")
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(self.home, mode=0o700)
        prev = os.environ.pop("META_API_KEY", None)
        if prev is not None:
            self.addCleanup(os.environ.__setitem__,
                            "META_API_KEY", prev)
        prev_bin = os.environ.pop("OPENMAINTY_MUSE_BINARY", None)
        if prev_bin is not None:
            self.addCleanup(os.environ.__setitem__,
                            "OPENMAINTY_MUSE_BINARY", prev_bin)
        popen = mock.patch.object(
            muse_mod.subprocess, "Popen",
            side_effect=AssertionError("no spawn in offline tests"))
        popen.start()
        self.addCleanup(popen.stop)

    def test_value_never_recorded_only_bool(self):
        with mock.patch.dict(os.environ,
                             {"MUSE_CONTRIBUTOR_MODE": SENTINEL}):
            ident = pf.current_identity()
            self.assertIs(
                ident["auth"]["contributor_env_present"], True)
            created = pf.create_preflight(self.home,
                                          "sentinel-world")
            out = pf.run_preflight(self.home, created["pfid"])
            self.assertEqual(out["state"], "refused")
        blob = json.dumps(ident, sort_keys=True)
        self.assertNotIn(SENTINEL, blob)
        found = 0
        for dirpath, _names, files in os.walk(self.home):
            for leaf in files:
                if not leaf.endswith(".json"):
                    continue
                with open(os.path.join(dirpath, leaf),
                          "rb") as fh:
                    raw = fh.read()
                self.assertNotIn(SENTINEL.encode("utf-8"), raw,
                                 leaf)
                found += 1
        self.assertGreater(found, 0)
        inputs, _raw = pf._load_doc(
            self.home, created["pfid"], "input",
            pf.PREFLIGHT_INPUT_MAX_BYTES)
        self.assertIs(
            inputs["auth_observed"]["contributor_env_present"],
            True)


def _legacy_auth():
    """Byte-exact pre-273 auth shape (4 keys, no contributor fact)."""
    return {"meta_api_key_present": False,
            "credential_source": "unknown",
            "billing_evidence": None,
            "observed_how": "app env presence check only; no "
                            "session proof observed"}


class LegacyCompatCase(unittest.TestCase):
    """Pre-273 stored records stay readable and never reusable."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pf273-")
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(self.home, mode=0o700)
        for var in ("META_API_KEY", "MUSE_CONTRIBUTOR_MODE",
                    "OPENMAINTY_MUSE_BINARY"):
            prev = os.environ.pop(var, None)
            if prev is not None:
                self.addCleanup(os.environ.__setitem__, var,
                                prev)
        popen = mock.patch.object(
            muse_mod.subprocess, "Popen",
            side_effect=AssertionError("no spawn in offline tests"))
        popen.start()
        self.addCleanup(popen.stop)

    def test_legacy_auth_shape_reads_not_corrupt(self):
        pf._validate_auth_shape(_legacy_auth(), "legacy.auth")
        for bad in ("yes", 1, None, 0):
            with self.subTest(bad=bad):
                auth = _legacy_auth()
                auth["contributor_env_present"] = bad
                with self.assertRaises(PreflightError) as cm:
                    pf._validate_auth_shape(auth, "legacy.auth")
                self.assertEqual(cm.exception.code, "corrupt")
        with_unknown = _legacy_auth()
        with_unknown["contributor_env_mode"] = "x"
        with self.assertRaises(PreflightError) as cm2:
            pf._validate_auth_shape(with_unknown, "legacy.auth")
        self.assertEqual(cm2.exception.code, "corrupt")

    def test_same_world_legacy_never_same(self):
        base = _fixture_values()
        live = json.loads(json.dumps(base))
        legacy = json.loads(json.dumps(base))
        legacy_auth = legacy["auth"]
        del legacy_auth["contributor_env_present"]
        recorded = {"binary": legacy["binary"],
                    "config": legacy["config"],
                    "source": legacy["source"],
                    "env": legacy["env"],
                    "auth_observed": legacy_auth,
                    "policy": dict(pf.REQUIRED_POLICY)}
        self.assertFalse(pf._same_world(recorded, live))
        live_true = json.loads(json.dumps(live))
        live_true["auth"]["contributor_env_present"] = True
        self.assertFalse(pf._same_world(recorded, live_true))
        fresh = json.loads(json.dumps(recorded))
        fresh["auth_observed"]["contributor_env_present"] = False
        self.assertTrue(pf._same_world(fresh, live))
        drifted = json.loads(json.dumps(fresh))
        drifted["auth_observed"]["contributor_env_present"] = True
        self.assertFalse(pf._same_world(drifted, live))

    def test_fingerprint_absent_differs_from_false(self):
        base = _fixture_values()
        legacy = json.loads(json.dumps(base))
        del legacy["auth"]["contributor_env_present"]
        full_false = muse_mod.source_repair_fingerprint(base)
        full_absent = muse_mod.source_repair_fingerprint(legacy)
        self.assertNotEqual(full_absent, full_false)
        trueish = json.loads(json.dumps(base))
        trueish["auth"]["contributor_env_present"] = True
        full_true = muse_mod.source_repair_fingerprint(trueish)
        self.assertNotEqual(full_true, full_false)
        self.assertNotEqual(full_true, full_absent)

    def test_observer_omits_key_for_legacy_inputs(self):
        inputs = {"auth_observed": _legacy_auth()}
        spawn = {"exit_code": 125, "output_bytes": 0,
                 "usage": {}}
        _channels, auth = pf._default_observer(
            self.home, "pf-legacy", inputs, spawn)
        self.assertNotIn("contributor_env_present", auth)
        self.assertEqual(auth["credential_source"], "unknown")
        # Unknown-auth path preserves legacy shape too.
        pf._validate_auth_shape(auth, "legacy.observer.auth")
        unknown = pf._unknown_auth(inputs)
        self.assertNotIn("contributor_env_present", unknown)

    def _write_legacy_receipt(self):
        """Pre-273 receipt via the real code path, key stripped.

        Runs create+run with an unestablished-identity mock (the
        269 code path for this world), then rewrites the input and
        outcome leaves with the contributor key deleted and the
        inputs pin recomputed: byte-shape identical to a 269 write.
        Returns (pfid, reason).
        """
        values = _fixture_values()
        values["config"] = {"status": "unestablished",
                            "files": [], "note": ""}
        with mock.patch("openmainty.preflight.current_identity",
                        return_value=values):
            created = pf.create_preflight(self.home,
                                          "legacy-269-world")
            out = pf.run_preflight(
                self.home, created["pfid"],
                transport=_fake_transport(),
                observer=_fixture_observer(
                    auth_source="unknown"))
        self.assertEqual(out["code"], "config-unestablished")
        pfid = created["pfid"]
        directory = os.path.join(self.home, "preflight")
        input_path = os.path.join(directory, f"{pfid}.input.json")
        with open(input_path, "rb") as fh:
            raw = fh.read()
        inputs = json.loads(raw.decode("utf-8"))
        del inputs["auth_observed"]["contributor_env_present"]
        stripped = json.dumps(inputs, sort_keys=True,
                              separators=(",", ":"),
                              ensure_ascii=False).encode("utf-8")
        with open(input_path, "wb") as fh:
            fh.write(stripped)
        outcome_path = os.path.join(
            directory, f"{pfid}.outcome.json")
        with open(outcome_path, "rb") as fh:
            outcome = json.loads(fh.read().decode("utf-8"))
        del outcome["auth"]["contributor_env_present"]
        outcome["inputs_sha256"] = hashlib.sha256(
            stripped).hexdigest()
        with open(outcome_path, "wb") as fh:
            fh.write(json.dumps(
                outcome, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False).encode("utf-8"))
        return pfid, outcome["reason"]

    def test_legacy_history_projects_refused_unchanged(self):
        pfid, reason = self._write_legacy_receipt()
        inputs, _raw = pf._load_doc(
            self.home, pfid, "input",
            pf.PREFLIGHT_INPUT_MAX_BYTES)
        self.assertNotIn("contributor_env_present",
                         inputs["auth_observed"])
        outcome, _raw2 = pf._load_doc(
            self.home, pfid, "outcome",
            pf.PREFLIGHT_OUTCOME_MAX_BYTES)
        self.assertNotIn("contributor_env_present",
                         outcome["auth"])
        got = pf.describe_preflight(self.home)
        self.assertEqual(got["state"], "not-verified")
        self.assertEqual(got["code"], "no-preflight")
        self.assertEqual(len(got["history"]), 1)
        entry = got["history"][0]
        self.assertEqual(entry["pfid"], pfid)
        self.assertEqual(entry["state"], "refused")
        self.assertEqual(entry["code"], "config-unestablished")
        self.assertEqual(entry["reason"], reason)
        self.assertFalse(got["authority_now"]["authorized"])
