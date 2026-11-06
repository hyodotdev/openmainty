"""Slice 297 (S1+S2): preflight identity from a live worker.

current_identity() hard-codes config "unestablished" and
credential_source "unknown". This slice adds the observer-backed
path: with a live subscription-broker worker handle, config
identity comes from the slice-293 worker record and the
credential source from the worker's own account observation
plus its owned trace (credential.status source=login,
masks_login=false). Anything else stays unestablished with
the worker's recorded reason. No model turn, no fixture path
in production: tests use scripted children at the existing
seams only.

Failing-first: every worker-backed test here fails on the
om353 tree (current_identity takes no handle; the worker
config key is corrupt; no binding comparison exists).
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__),
                               "..", "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from openmainty import muse as muse_mod  # noqa: E402
from openmainty import preflight as preflight_mod  # noqa: E402
from test_273_followups import (  # noqa: E402
    Worker273Mixin, _msp_test_err)
from test_preflight import (  # noqa: E402
    _MspScriptedChild, _fake_transport, _fixture_observer,
    _fixture_values, _msp_test_init_result, _msp_test_ok)

# Captured before any mock: authority tests re-patch
# current_identity over PreflightCase's fixture patch.
_REAL_CURRENT_IDENTITY = preflight_mod.current_identity

ACCOUNT_LOGIN = {"state": "accountLogin",
                 "credentialRequired": True}

CRED_LINE = ('ts=1 event="credential.status" provider="meta" '
             'source="login" available=true login_present=true '
             'masks_login=false duration_ms=3')

CRED_OTHER = ('ts=2 event="credential.status" provider="other" '
              'source="login" masks_login=false')

FAKE_SECRET = "FakeSecretTokenValue24680"


def _settings_result(**over):
    values = {"provider": "meta",
              "model": "muse-spark-1.3",
              "reasoning_effort": "high",
              "contributor_mode_enabled": True}
    values.update(over)
    return {"schemaVersion": 1, "values": values}


class _LiveWorkerMixin(Worker273Mixin):
    def _spawn_root(self):
        root_tmp = tempfile.TemporaryDirectory(
            prefix="msp297-root-")
        self.addCleanup(root_tmp.cleanup)
        root = os.path.realpath(root_tmp.name)
        os.chmod(root, 0o700)
        return root

    def _live_worker(self, profile="subscription-broker",
                     account=None, settings=None,
                     trace_lines=None):
        """Bootstrap+session+settings on a scripted worker.

        The initial settings/read takes id 4; each later
        observation sends its own fresh settings/read, so
        call _observe with the next request id (5, 6, ...).
        """
        self._stop_worker_patches()
        root = self._spawn_root()
        child = _MspScriptedChild(script={})
        self.addCleanup(child.force_close)
        self._spawn_with(child)
        handle = muse_mod.spawn_msp_worker(
            root, profile=profile)
        self.addCleanup(self._close_quiet, handle)
        home = os.path.join(root, "data", "muse")
        child._script.update({
            "initialize": [_msp_test_ok(
                1, _msp_test_init_result(home))],
            "account/read": [_msp_test_ok(
                2, dict(ACCOUNT_LOGIN if account is None
                        else account))]})
        muse_mod.msp_worker_bootstrap(handle, timeout=10,
                                      app_version="t")
        self.root = root
        self._start_session(handle, child, 3)
        child._script["settings/read"] = [_msp_test_ok(
            4, _settings_result() if settings is None
            else settings)]
        muse_mod.msp_worker_settings_read(handle, timeout=10)
        if trace_lines is not None:
            tracedir = os.path.join(
                root, "data", "muse", "local-tracing")
            os.makedirs(tracedir, mode=0o700, exist_ok=True)
            with open(os.path.join(tracedir, "t.log"), "w",
                      encoding="utf-8") as fh:
                fh.write("\n".join(trace_lines) + "\n")
        return handle, child

    def _observe(self, handle, child, rid, settings=None):
        """Script one settings/read reply, then observe."""
        child._script["settings/read"] = [_msp_test_ok(
            rid, _settings_result() if settings is None
            else settings)]
        return preflight_mod.current_identity(
            worker_handle=handle)

    def _close_quiet(self, handle):
        try:
            muse_mod.msp_worker_close(handle, reason="t")
        except muse_mod.MspError:
            pass


class WorkerIdentityCase(_LiveWorkerMixin, unittest.TestCase):
    def setUp(self):
        self._worker_fixture()

    def test_default_identity_is_hardcoded_unestablished(self):
        live = preflight_mod.current_identity()
        self.assertEqual(live["config"]["status"],
                         "unestablished")
        self.assertEqual(live["config"]["files"], [])
        self.assertEqual(live["auth"]["credential_source"],
                         "unknown")
        self.assertIsNone(live["auth"]["billing_evidence"])

    def test_bad_handle_type_raises(self):
        with self.assertRaises(ValueError):
            preflight_mod.current_identity(worker_handle=123)

    def test_unknown_handle_is_unestablished(self):
        live = preflight_mod.current_identity(
            worker_handle="mspw-no-such-handle")
        self.assertEqual(live["config"]["status"],
                         "unestablished")
        self.assertIn("gone", live["config"]["note"])
        self.assertEqual(live["auth"]["credential_source"],
                         "unknown")

    def test_established_from_live_worker(self):
        handle, child = self._live_worker(
            trace_lines=[CRED_LINE])
        try:
            live = self._observe(handle, child, 5)
            rec = muse_mod._MSP_WORKERS[handle]
            ident = rec["config_identity"]
            config = live["config"]
            self.assertEqual(config["status"], "established")
            self.assertEqual(config["files"], [])
            worker = config["worker"]
            self.assertEqual(worker["handle"], handle)
            self.assertEqual(worker["pid"], rec["child_pid"])
            self.assertEqual(worker["sha256"],
                             ident["sha256"])
            self.assertEqual(worker["provider"], "meta")
            self.assertEqual(worker["model"], "muse-spark-1.3")
            self.assertIs(worker["contributor"], True)
            auth = live["auth"]
            self.assertIs(auth["meta_api_key_present"],
                          False)
            self.assertEqual(auth["credential_source"],
                             "subscription-login")
            self.assertIsNone(auth["billing_evidence"])
            self.assertIn("credential.status",
                          auth["observed_how"])
            self.assertIn("billing not observed",
                          auth["observed_how"])
            # Closed shapes accept the new observation.
            preflight_mod._validate_config_shape(
                config, "test.config")
            preflight_mod._validate_auth_shape(
                auth, "test.auth")
        finally:
            self._close_quiet(handle)

    def test_wrong_profile_is_unestablished(self):
        handle, child = self._live_worker(
            profile="subscription-canary", trace_lines=[])
        try:
            live = self._observe(handle, child, 5)
            self.assertEqual(live["config"]["status"],
                             "unestablished")
            self.assertIn("subscription-canary",
                          live["config"]["note"])
            self.assertEqual(live["auth"]["credential_source"],
                             "unknown")
        finally:
            self._close_quiet(handle)

    def test_config_mismatch_keeps_worker_reason(self):
        handle, child = self._live_worker(
            settings=_settings_result(provider="other"),
            trace_lines=[CRED_LINE])
        try:
            live = self._observe(
                handle, child, 5,
                _settings_result(provider="other"))
            rec = muse_mod._MSP_WORKERS[handle]
            self.assertIsNot(
                rec.get("config_established"), True)
            self.assertEqual(live["config"]["status"],
                             "unestablished")
            self.assertIn(rec["config_unset_reason"],
                          live["config"]["note"])
        finally:
            self._close_quiet(handle)

    def test_worker_gone_is_unestablished(self):
        handle, _child = self._live_worker(
            trace_lines=[CRED_LINE])
        self._close_quiet(handle)
        live = preflight_mod.current_identity(
            worker_handle=handle)
        self.assertEqual(live["config"]["status"],
                         "unestablished")
        self.assertIn("gone", live["config"]["note"])

    def test_pid_change_is_unestablished(self):
        handle, child = self._live_worker(
            trace_lines=[CRED_LINE])
        rec = muse_mod._MSP_WORKERS[handle]
        real_pid = rec["child_pid"]
        try:
            rec["child_pid"] = real_pid + 1000000
            live = self._observe(handle, child, 5)
            self.assertEqual(live["config"]["status"],
                             "unestablished")
            self.assertIn("pid", live["config"]["note"])
        finally:
            rec["child_pid"] = real_pid
            self._close_quiet(handle)

    def test_missing_trace_event_keeps_auth_unknown(self):
        handle, child = self._live_worker(trace_lines=[
            'ts=1 event="session.started" duration_ms=1'])
        try:
            live = self._observe(handle, child, 5)
            # Config is proved by the worker record alone;
            # only the credential leg stays unestablished.
            self.assertEqual(live["config"]["status"],
                             "established")
            self.assertEqual(live["auth"]["credential_source"],
                             "unknown")
            self.assertIn("credential.status",
                          live["auth"]["observed_how"])
        finally:
            self._close_quiet(handle)

    def test_masks_login_true_keeps_auth_unknown(self):
        handle, child = self._live_worker(trace_lines=[
            CRED_LINE.replace("masks_login=false",
                              "masks_login=true")])
        try:
            live = self._observe(handle, child, 5)
            self.assertEqual(live["config"]["status"],
                             "established")
            self.assertEqual(live["auth"]["credential_source"],
                             "unknown")
            self.assertIn("masks_login",
                          live["auth"]["observed_how"])
        finally:
            self._close_quiet(handle)

    def test_latest_credential_line_wins(self):
        masked = CRED_LINE.replace("masks_login=false",
                                   "masks_login=true")
        handle, child = self._live_worker(
            trace_lines=[masked, CRED_LINE])
        try:
            live = self._observe(handle, child, 5)
            self.assertEqual(live["auth"]["credential_source"],
                             "subscription-login")
        finally:
            self._close_quiet(handle)
        handle2, child2 = self._live_worker(
            trace_lines=[CRED_LINE, masked])
        try:
            live2 = self._observe(handle2, child2, 5)
            self.assertEqual(live2["auth"]["credential_source"],
                             "unknown")
        finally:
            self._close_quiet(handle2)

    def test_api_key_lane_keeps_auth_unknown(self):
        handle, child = self._live_worker(
            account={"state": "apiKey",
                     "credentialRequired": True},
            trace_lines=[CRED_LINE])
        try:
            live = self._observe(handle, child, 5)
            self.assertEqual(live["config"]["status"],
                             "established")
            self.assertEqual(live["auth"]["credential_source"],
                             "unknown")
            self.assertIn("apiKey", live["auth"]["observed_how"])
        finally:
            self._close_quiet(handle)

    def test_env_key_lane_keeps_auth_unknown(self):
        handle, child = self._live_worker(
            account={"state": "envKey",
                     "credentialRequired": True},
            trace_lines=[CRED_LINE])
        try:
            live = self._observe(handle, child, 5)
            self.assertEqual(live["auth"]["credential_source"],
                             "unknown")
            self.assertIn("envKey", live["auth"]["observed_how"])
        finally:
            self._close_quiet(handle)

    def test_logged_out_lane_keeps_auth_unknown(self):
        handle, child = self._live_worker(
            account={"state": "loggedOut",
                     "credentialRequired": True},
            trace_lines=[CRED_LINE])
        try:
            live = self._observe(handle, child, 5)
            self.assertEqual(live["auth"]["credential_source"],
                             "unknown")
        finally:
            self._close_quiet(handle)

    def test_meta_api_key_presence_still_refuses(self):
        handle, child = self._live_worker(
            trace_lines=[CRED_LINE])
        try:
            with mock.patch.dict(os.environ,
                                 {"META_API_KEY": "x"}):
                live = self._observe(handle, child, 5)
            self.assertIs(live["auth"]["meta_api_key_present"],
                          True)
            self.assertEqual(live["auth"]["credential_source"],
                             "unknown")
        finally:
            self._close_quiet(handle)

    def test_trace_secret_never_enters_records(self):
        handle, child = self._live_worker(trace_lines=[
            CRED_LINE + " token=" + FAKE_SECRET,
            'event="credential.status" source="login" '
            'masks_login=false note="' + FAKE_SECRET + '"'])
        try:
            live = self._observe(handle, child, 5)
            blob = json.dumps(live, sort_keys=True)
            self.assertNotIn(FAKE_SECRET, blob)
            # The login leg still proves: unknown keys drop.
            self.assertEqual(live["auth"]["credential_source"],
                             "subscription-login")
        finally:
            self._close_quiet(handle)

    def test_broken_worker_is_unestablished(self):
        handle, child = self._live_worker(
            trace_lines=[CRED_LINE])
        try:
            live = self._observe(handle, child, 5)
            self.assertEqual(live["config"]["status"],
                             "established")
            muse_mod._MSP_WORKERS[handle][
                "broken"] = "account-changed"
            dead = preflight_mod.current_identity(
                worker_handle=handle)
            self.assertEqual(dead["config"]["status"],
                             "unestablished")
            self.assertIn("broken", dead["config"]["note"])
        finally:
            self._close_quiet(handle)

    def test_exited_proc_is_unestablished(self):
        handle, child = self._live_worker(
            trace_lines=[CRED_LINE])
        try:
            live = self._observe(handle, child, 5)
            self.assertEqual(live["config"]["status"],
                             "established")
            child.poll = lambda: 0
            try:
                dead = preflight_mod.current_identity(
                    worker_handle=handle)
            finally:
                del child.poll
            self.assertEqual(dead["config"]["status"],
                             "unestablished")
            self.assertIn("exited", dead["config"]["note"])
        finally:
            self._close_quiet(handle)

    def test_owner_mismatch_is_unestablished(self):
        handle, child = self._live_worker(
            trace_lines=[CRED_LINE])
        rec = muse_mod._MSP_WORKERS[handle]
        real_owner = rec["owner_pid"]
        try:
            live = self._observe(handle, child, 5)
            self.assertEqual(live["config"]["status"],
                             "established")
            rec["owner_pid"] = real_owner + 1
            dead = preflight_mod.current_identity(
                worker_handle=handle)
            self.assertEqual(dead["config"]["status"],
                             "unestablished")
            self.assertIn("owner", dead["config"]["note"])
        finally:
            rec["owner_pid"] = real_owner
            self._close_quiet(handle)

    def test_settings_read_error_is_unestablished(self):
        handle, child = self._live_worker(
            trace_lines=[CRED_LINE])
        try:
            child._script["settings/read"] = [
                _msp_test_err(5)]
            live = preflight_mod.current_identity(
                worker_handle=handle)
            self.assertEqual(live["config"]["status"],
                             "unestablished")
            self.assertIn("settings-read",
                          live["config"]["note"])
        finally:
            self._close_quiet(handle)

    def test_reread_changed_settings_unestablished(self):
        handle, child = self._live_worker(
            trace_lines=[CRED_LINE])
        try:
            live = self._observe(handle, child, 5)
            self.assertEqual(live["config"]["status"],
                             "established")
            stale = self._observe(
                handle, child, 6,
                _settings_result(provider="other"))
            self.assertEqual(stale["config"]["status"],
                             "unestablished")
            # The slice-293 evaluator's own reason, verbatim.
            self.assertIn("provider-mismatch",
                          stale["config"]["note"])
        finally:
            self._close_quiet(handle)

    def test_non_meta_credential_line_ignored(self):
        handle, child = self._live_worker(
            trace_lines=[CRED_OTHER])
        try:
            live = self._observe(handle, child, 5)
            self.assertEqual(live["auth"]["credential_source"],
                             "unknown")
            self.assertIn("credential-status-missing",
                          live["auth"]["observed_how"])
        finally:
            self._close_quiet(handle)

    def test_other_provider_line_does_not_decide(self):
        masked_other = CRED_OTHER.replace(
            "masks_login=false", "masks_login=true")
        handle, child = self._live_worker(
            trace_lines=[CRED_LINE, masked_other])
        try:
            live = self._observe(handle, child, 5)
            self.assertEqual(live["auth"]["credential_source"],
                             "subscription-login")
        finally:
            self._close_quiet(handle)

    def test_trace_past_cap_reads_tail(self):
        masked = CRED_LINE.replace("masks_login=false",
                                   "masks_login=true")
        handle, child = self._live_worker(
            trace_lines=["pad"])
        try:
            tracedir = os.path.join(
                self.root, "data", "muse", "local-tracing")
            big = (CRED_LINE + "\n"
                   + 'event="heartbeat" n=1\n' * 60000
                   + masked + "\n")
            self.assertGreater(len(big), 1 << 20)
            with open(os.path.join(tracedir, "t.log"), "w",
                      encoding="utf-8") as fh:
                fh.write(big)
            live = self._observe(handle, child, 5)
            self.assertEqual(live["auth"]["credential_source"],
                             "unknown")
            self.assertIn("masks_login",
                          live["auth"]["observed_how"])
        finally:
            self._close_quiet(handle)

    def test_too_many_trace_files_unestablished(self):
        handle, child = self._live_worker(
            trace_lines=["pad"])
        try:
            tracedir = os.path.join(
                self.root, "data", "muse", "local-tracing")
            for idx in range(2, 10):
                with open(os.path.join(
                        tracedir, "t%d.log" % idx), "w",
                        encoding="utf-8") as fh:
                    fh.write("pad\n")
            live = self._observe(handle, child, 5)
            self.assertEqual(live["auth"]["credential_source"],
                             "unknown")
            self.assertIn("trace-too-many-files",
                          live["auth"]["observed_how"])
        finally:
            self._close_quiet(handle)


class BindingCase(Worker273Mixin, unittest.TestCase):
    def setUp(self):
        self._worker_fixture()

    def _world(self, config, source):
        return {
            "binary": {"path": "p", "real_path": "r",
                       "sha256": "s"},
            "config": config,
            "source": {"app_version": "v", "files": []},
            "env": {"passthrough_keys": [], "cache_keys": [],
                    "values": {}},
            "auth_observed": {
                "meta_api_key_present": False,
                "credential_source": source},
            "auth": {"meta_api_key_present": False,
                     "credential_source": source},
            "policy": dict(preflight_mod.REQUIRED_POLICY),
        }

    def _binding(self, **over):
        triple = {"handle": "mspw-test-1", "pid": 100,
                  "sha256": "a" * 64, "provider": "meta",
                  "model": "muse-spark-1.3",
                  "contributor": True}
        triple.update(over)
        return triple

    def _established(self, binding):
        config = {"status": "established", "files": [],
                  "note": "worker-proved"}
        if binding is not None:
            config["worker"] = binding
        return config

    def test_same_binding_is_same_world(self):
        recorded = self._world(
            self._established(self._binding()),
            "subscription-login")
        live = self._world(
            self._established(self._binding()),
            "subscription-login")
        self.assertTrue(
            preflight_mod._same_world(recorded, live))

    def test_pid_change_is_new_world(self):
        recorded = self._world(
            self._established(self._binding()),
            "subscription-login")
        live = self._world(
            self._established(self._binding(pid=101)),
            "subscription-login")
        self.assertFalse(
            preflight_mod._same_world(recorded, live))

    def test_sha_drift_is_new_world(self):
        recorded = self._world(
            self._established(self._binding()),
            "subscription-login")
        live = self._world(
            self._established(self._binding(
                sha256="b" * 64)),
            "subscription-login")
        self.assertFalse(
            preflight_mod._same_world(recorded, live))

    def test_handle_change_is_new_world(self):
        recorded = self._world(
            self._established(self._binding()),
            "subscription-login")
        live = self._world(
            self._established(self._binding(
                handle="mspw-test-2")),
            "subscription-login")
        self.assertFalse(
            preflight_mod._same_world(recorded, live))

    def test_binding_presence_mismatch_is_new_world(self):
        recorded = self._world(
            self._established(self._binding()),
            "subscription-login")
        live = self._world(self._established(None),
                           "subscription-login")
        self.assertFalse(
            preflight_mod._same_world(recorded, live))
        self.assertFalse(
            preflight_mod._same_world(live, recorded))

    def test_legacy_without_binding_unchanged(self):
        recorded = self._world(
            {"status": "unestablished", "files": [],
             "note": "t"},
            "unknown")
        live = self._world(
            {"status": "unestablished", "files": [],
             "note": "t"},
            "unknown")
        self.assertTrue(
            preflight_mod._same_world(recorded, live))

    def test_worker_config_shape_closed(self):
        good = self._established(self._binding())
        preflight_mod._validate_config_shape(
            good, "test.config")
        bad = self._established(self._binding())
        bad["worker"]["extra"] = 1
        with self.assertRaises(
                preflight_mod.PreflightError) as cm:
            preflight_mod._validate_config_shape(
                bad, "test.config")
        self.assertEqual(cm.exception.code, "corrupt")
        bad_pid = self._established(self._binding(pid="1"))
        with self.assertRaises(
                preflight_mod.PreflightError):
            preflight_mod._validate_config_shape(
                bad_pid, "test.config")
        zero_pid = self._established(self._binding(pid=0))
        with self.assertRaises(
                preflight_mod.PreflightError) as cm0:
            preflight_mod._validate_config_shape(
                zero_pid, "test.config")
        self.assertEqual(cm0.exception.code, "corrupt")

    def test_next_refusal_is_channels_not_verified(self):
        config = self._established(self._binding())
        auth = {"meta_api_key_present": False,
                "credential_source": "subscription-login",
                "billing_evidence": None,
                "observed_how": "test worker proof"}
        channels = preflight_mod._unknown_channels(
            "no positive control")
        state, _reason, code = preflight_mod.evaluate_outcome(
            {"policy": dict(preflight_mod.REQUIRED_POLICY),
             "config": config},
            channels, auth)
        self.assertEqual(state, "unknown")
        self.assertEqual(code, "channel-unknown")

    def test_native_leg_still_refuses(self):
        with self.assertRaises(
                preflight_mod.PreflightError) as cm:
            preflight_mod.native_containment()
        self.assertEqual(cm.exception.code,
                         "native-containment-unproved")


class AuthorityBindingCase(_LiveWorkerMixin, unittest.TestCase):
    def setUp(self):
        self._worker_fixture()
        self.ptmp = tempfile.TemporaryDirectory(
            prefix="pf297-")
        self.addCleanup(self.ptmp.cleanup)
        self.home = os.path.join(self.ptmp.name, "home")
        os.makedirs(self.home, mode=0o700)

    def _fix_identity(self, handle, child=None, rid=None,
                      settings=None):
        """Fixture legs + REAL worker config/auth legs.

        A live handle needs its next settings/read reply
        scripted (each observation re-reads the worker);
        pass no child for a gone handle (no wire happens).
        """
        if child is not None:
            child._script["settings/read"] = [_msp_test_ok(
                rid, _settings_result() if settings is None
                else settings)]
        live = _REAL_CURRENT_IDENTITY(handle)
        fixed = _fixture_values()
        fixed["config"] = live["config"]
        fixed["auth"] = live["auth"]
        return fixed

    def test_authority_binds_same_worker_only(self):
        handle, child = self._live_worker(
            trace_lines=[CRED_LINE])
        try:
            recorded = self._fix_identity(handle, child, 5)
            self.assertEqual(
                recorded["config"]["status"], "established")
            with mock.patch(
                    "openmainty.preflight.current_identity",
                    return_value=recorded):
                created = preflight_mod.create_preflight(
                    self.home, dedupe_key="w1")
                out = preflight_mod.run_preflight(
                    self.home, created["pfid"],
                    transport=_fake_transport(),
                    observer=_fixture_observer(
                        auth_source="subscription-login"))
            self.assertEqual(out["state"], "verified")
            # Same worker alive: authorized (no raise).
            ok = preflight_mod.check_target_authority(
                self.home,
                current=self._fix_identity(handle, child, 6))
            self.assertEqual(ok["pfid"], created["pfid"])
            self.assertIn("verified_at", ok)
        finally:
            self._close_quiet(handle)
        # Worker gone: the result no longer counts.
        with self.assertRaises(
                preflight_mod.PreflightError) as cm:
            preflight_mod.check_target_authority(
                self.home,
                current=self._fix_identity(handle))
        self.assertEqual(cm.exception.code,
                         "config-unestablished")
        # A second worker is not the same proof.
        handle2, child2 = self._live_worker(
            trace_lines=[CRED_LINE])
        try:
            with self.assertRaises(
                    preflight_mod.PreflightError) as cm2:
                preflight_mod.check_target_authority(
                    self.home,
                    current=self._fix_identity(
                        handle2, child2, 5))
            self.assertEqual(cm2.exception.code,
                             "config-unestablished")
        finally:
            self._close_quiet(handle2)

    def _record_verified(self, handle, child, rid):
        recorded = self._fix_identity(handle, child, rid)
        self.assertEqual(recorded["config"]["status"],
                         "established")
        with mock.patch(
                "openmainty.preflight.current_identity",
                return_value=recorded):
            created = preflight_mod.create_preflight(
                self.home, dedupe_key="w1")
            out = preflight_mod.run_preflight(
                self.home, created["pfid"],
                transport=_fake_transport(),
                observer=_fixture_observer(
                    auth_source="subscription-login"))
        self.assertEqual(out["state"], "verified")
        return created

    def test_authority_refuses_after_break(self):
        handle, child = self._live_worker(
            trace_lines=[CRED_LINE])
        try:
            self._record_verified(handle, child, 5)
            muse_mod._MSP_WORKERS[handle][
                "broken"] = "account-changed"
            with self.assertRaises(
                    preflight_mod.PreflightError) as cm:
                preflight_mod.check_target_authority(
                    self.home,
                    current=self._fix_identity(handle))
            self.assertEqual(cm.exception.code,
                             "config-unestablished")
        finally:
            self._close_quiet(handle)

    def test_authority_refuses_after_settings_change(self):
        handle, child = self._live_worker(
            trace_lines=[CRED_LINE])
        try:
            self._record_verified(handle, child, 5)
            changed = self._fix_identity(
                handle, child, 6,
                _settings_result(provider="other"))
            self.assertEqual(changed["config"]["status"],
                             "unestablished")
            with self.assertRaises(
                    preflight_mod.PreflightError) as cm:
                preflight_mod.check_target_authority(
                    self.home, current=changed)
            self.assertEqual(cm.exception.code,
                             "config-unestablished")
        finally:
            self._close_quiet(handle)

    def test_create_refuses_non_dict_worker(self):
        bad = _fixture_values()
        bad["config"]["worker"] = "mspw-not-a-dict"
        with mock.patch(
                "openmainty.preflight.current_identity",
                return_value=bad):
            with self.assertRaises(
                    preflight_mod.PreflightError) as cm:
                preflight_mod.create_preflight(
                    self.home, dedupe_key="wbad")
        self.assertEqual(cm.exception.code, "corrupt")
