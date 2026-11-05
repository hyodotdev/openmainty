"""Offline regressions for the app-owned capability preflight (Stage2B).

Uses synthetic local fixtures only (temp owned home, no network, no
target repos). The ONLY stubbed boundaries are the model transport
(fake callables with the exec_source_repair tuple shape), the
subscription-availability probe for serve routes, the I/O boundary
(current_identity / the channel observer, mocked under fresh owned
fixtures), and a Popen sentinel that fails any test that would
spawn. Every comparison, schema check, fail-closed branch, claim,
lease, and record write runs for real: source/identity validators
are never patched and no real model/login calls happen. All handler
assertions call the real product functions directly against fresh
owned fixtures. Tests are AUTHORED, not executed, in the authoring
gate; Root runs them fenced.
"""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib import request as urlrequest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from openmainty import muse
from openmainty import preflight as pf
from openmainty import sched
from openmainty import serve


def _fixture_values():
    """Deterministic owned identity facts (mocked I/O boundary)."""
    return {
        "binary": {"status": "ok", "path": "/fixture/muse",
                   "real_path": "/fixture/muse",
                   "sha256": "ab" * 32},
        "policy": dict(pf.REQUIRED_POLICY),
        "config": {"status": "established",
                   "files": [{"path": "conf", "sha256": "cd" * 32}]},
        "source": {"status": "ok", "app_version": "fx-1",
                   "files": [{"path": name, "sha256": "ef" * 32}
                             for name in pf._MANAGED_SOURCE_FILES]},
        "env": pf._child_env_shape(),
        "auth": {"meta_api_key_present": False,
                 "contributor_env_present": False,
                 "credential_source": "owned-fixture-observer",
                 "billing_evidence": None,
                 "observed_how": "fixture"},
    }


def _fixture_observer(auth_source="owned-fixture-observer",
                      unknown_channels=False):
    """Explicit fake observer: controlled typed verdicts (tests only).

    unknown_channels=True reports every channel as unproved
    (observed unknown, positive False) while keeping auth
    established — the honest "ran but unproved" branch, distinct
    from the default observer's auth-unknown refusal.
    """
    def _observe(home_path, pfid, inputs, spawn):
        channels = {}
        for name in pf.REQUIRED_CHANNELS:
            if unknown_channels:
                channels[name] = {
                    "required": pf.REQUIRED_DIRECTION[name],
                    "observed": "unknown",
                    "positive": False,
                    "detail": "fixture observer: no startup marker",
                }
                continue
            channels[name] = {
                "required": pf.REQUIRED_DIRECTION[name],
                "observed": pf.REQUIRED_DIRECTION[name],
                "positive": True,
                "detail": "fixture observer: controlled startup "
                          "marker present",
            }
        auth = {"meta_api_key_present": False,
                "contributor_env_present": False,
                "credential_source": auth_source,
                "billing_evidence": None,
                "observed_how": "fixture observer"}
        return channels, auth
    return _observe


def _fake_transport(exit_code=0, calls=None, profile_ok=True):
    def _transport(prompt, workdir, timeout, env, cancel):
        if calls is not None:
            calls.append((prompt, workdir))
        usage = {"requested_profile": dict(muse.SOURCE_REPAIR_PROFILE)} \
            if profile_ok else {}
        return (exit_code, "{}", usage)
    return _transport


class PreflightCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(self.home, mode=0o700)
        # Real (never executed) fixture binary: resolve_pinned_muse
        # exercises dual-identity + bounded reads against a real
        # regular file instead of an unconfigured path. Record/live
        # preflight identity still flows through the mocked I/O
        # boundary (current_identity), never ambient reads.
        bindir = os.path.join(self.tmp.name, "bin")
        os.makedirs(bindir, mode=0o700)
        self.bin = os.path.join(bindir, "muse")
        with open(self.bin, "wb") as fh:
            fh.write(b"fixture-muse-binary-v1")
        os.chmod(self.bin, 0o755)
        prev_bin = os.environ.get("OPENMAINTY_MUSE_BINARY")
        os.environ["OPENMAINTY_MUSE_BINARY"] = self.bin
        if prev_bin is None:
            self.addCleanup(os.environ.pop, "OPENMAINTY_MUSE_BINARY",
                            None)
        else:
            self.addCleanup(os.environ.__setitem__,
                            "OPENMAINTY_MUSE_BINARY", prev_bin)
        prev_home = os.environ.get("OPENMAINTY_HOME")
        os.environ["OPENMAINTY_HOME"] = self.home
        if prev_home is None:
            self.addCleanup(os.environ.pop, "OPENMAINTY_HOME", None)
        else:
            self.addCleanup(os.environ.__setitem__, "OPENMAINTY_HOME",
                            prev_home)
        # Deterministic credential posture: ambient keys would flip
        # every authority gate, so scrub for the fixture (the api-key
        # test sets its own value explicitly).
        prev_key = os.environ.pop("META_API_KEY", None)
        if prev_key is not None:
            self.addCleanup(os.environ.__setitem__, "META_API_KEY",
                            prev_key)
        # Same for the contributor-mode var: an ambient value would
        # flip the dedicated-spawn gate the same way.
        prev_contrib = os.environ.pop("MUSE_CONTRIBUTOR_MODE", None)
        if prev_contrib is not None:
            self.addCleanup(os.environ.__setitem__,
                            "MUSE_CONTRIBUTOR_MODE", prev_contrib)
        self.values = _fixture_values()
        cur = mock.patch("openmainty.preflight.current_identity",
                         return_value=self.values)
        cur.start()
        self.addCleanup(cur.stop)

    def _no_spawn(self):
        """Sentinel: any Popen in this block fails the test.

        Scoped per exec test (never global: the bounded-pipe test
        below must spawn fresh OWNED interpreter children).
        """
        popen = mock.patch.object(
            muse.subprocess, "Popen",
            side_effect=AssertionError("no spawn in offline tests"))
        popen.start()
        self.addCleanup(popen.stop)

    def _create(self, dedupe="k1"):
        self._no_spawn()
        return pf.create_preflight(self.home, dedupe)

    def _run_verified(self, dedupe="k1", calls=None):
        self._no_spawn()
        created = pf.create_preflight(self.home, dedupe)
        out = pf.run_preflight(
            self.home, created["pfid"],
            transport=_fake_transport(calls=calls),
            observer=_fixture_observer())
        self.assertEqual(out["state"], "verified")
        return created["pfid"]

    def test_create_and_reuse_live(self):
        first = self._create("same-world")
        self.assertFalse(first["reused"])
        again = pf.create_preflight(self.home, "same-world")
        self.assertTrue(again["reused"])
        self.assertEqual(again["pfid"], first["pfid"])
        self.assertEqual(again["state"], "live")
        other = self._create("other-world")
        self.assertFalse(other["reused"])
        self.assertNotEqual(other["pfid"], first["pfid"])

    def test_verified_reuse_requires_same_world(self):
        # A verified receipt reuses by key ONLY while the recorded
        # world still equals the live world: a config change must
        # start a fresh check, never relabel historical proof.
        first = self._run_verified("drift-reuse")
        again = pf.create_preflight(self.home, "drift-reuse")
        self.assertTrue(again["reused"])
        self.assertEqual(again["pfid"], first)
        live = json.loads(json.dumps(self.values))
        live["config"] = {"status": "established",
                          "files": [{"path": "other",
                                     "sha256": "00" * 32}]}
        with mock.patch("openmainty.preflight.current_identity",
                        return_value=live):
            second = pf.create_preflight(self.home, "drift-reuse")
        self.assertFalse(second.get("reused"))
        self.assertNotEqual(second["pfid"], first)
        # The historical receipt survives untouched: nothing rebound.
        latest = pf.latest_verified(self.home)
        self.assertIsNotNone(latest)
        self.assertEqual(latest[0], first)

    def test_create_rejects_bad_dedupe(self):
        for bad in ("", "x" * 129, None, 42, ["k"]):
            with self.assertRaises(pf.PreflightError) as cm:
                pf.create_preflight(self.home, bad)
            self.assertEqual(cm.exception.status, 400)
            self.assertEqual(cm.exception.code, "bad-dedupe-key")

    def test_create_validates_before_store(self):
        with mock.patch("openmainty.preflight.current_identity",
                        return_value={"bogus": 1}):
            with self.assertRaises(pf.PreflightError):
                pf.create_preflight(self.home, "bad-world")
        # Empty scan is ([], complete=True): a missing layout is
        # legitimate absence, not a truncated slice.
        self.assertEqual(pf._scan_pfids(self.home), ([], True))

    def test_run_verified_full_lifecycle(self):
        pfid = self._run_verified()
        latest = pf.latest_verified(self.home)
        self.assertIsNotNone(latest)
        self.assertEqual(latest[0], pfid)
        self.assertEqual(latest[2].get("state"), "verified")
        authority = pf.check_target_authority(self.home)
        self.assertEqual(authority["pfid"], pfid)
        self.assertEqual(len(authority["inputs_sha256"]), 64)
        described = pf.describe_preflight(self.home)
        self.assertEqual(described["state"], "verified")
        self.assertTrue(
            described["authority_now"].get("authorized"))
        self.assertEqual(
            described["authority_now"].get("pfid"), pfid)

    def test_rc0_default_observer_refuses_auth_unknown(self):
        """Branch A: rc0 + perfect requested flags MUST NOT verify.

        The default observer proves nothing (auth unknown), so the
        run records an honest REFUSAL with auth-unestablished. The
        persisted outcome carries that exact reason — but the GATE
        cannot re-derive auth from nothing verified: with no
        verified receipt it reports no-preflight (404), never a
        promoted rc0 and never a fabricated auth verdict.
        """
        created = self._create("rc0-world")
        out = pf.run_preflight(
            self.home, created["pfid"],
            transport=_fake_transport(exit_code=0))
        self.assertEqual(out["state"], "refused")
        self.assertEqual(out["code"], "auth-unestablished")
        # The refusal is the persisted record — not just a return
        # value: an rc0 spawn with perfect requested flags leaves a
        # refused outcome, never a promoted proof.
        stored, _raw = pf._load_doc(
            self.home, created["pfid"], "outcome",
            pf.PREFLIGHT_OUTCOME_MAX_BYTES)
        self.assertEqual(stored["state"], "refused")
        self.assertEqual(stored["code"], "auth-unestablished")
        self.assertTrue(stored["reason"])
        described = pf.describe_preflight(self.home)
        self.assertEqual(described["state"], "not-verified")
        self.assertEqual(described["code"], "no-preflight")
        with self.assertRaises(pf.PreflightError) as cm:
            pf.check_target_authority(self.home)
        self.assertEqual(cm.exception.code, "no-preflight")
        self.assertEqual(cm.exception.status, 404)

    def test_rc0_unknown_channels_stay_unknown(self):
        """Branch B: rc0 with established auth but unproved channels.

        The run records honest "unknown" (channel-unknown), distinct
        from branch A's refusal. The gate still refuses target-A:
        only a verified receipt authorizes, so an unknown outcome
        reads as no-preflight — never verified, never the auth code.
        """
        created = self._create("rc0-chan-world")
        out = pf.run_preflight(
            self.home, created["pfid"],
            transport=_fake_transport(exit_code=0),
            observer=_fixture_observer(unknown_channels=True))
        self.assertEqual(out["state"], "unknown")
        self.assertEqual(out["code"], "channel-unknown")
        # Persisted unknown is its own branch: recorded, readable,
        # but authorizing nothing. The gate must NOT report the
        # auth code for a channel-unknown receipt, and must NOT
        # verify on rc0.
        stored, _raw = pf._load_doc(
            self.home, created["pfid"], "outcome",
            pf.PREFLIGHT_OUTCOME_MAX_BYTES)
        self.assertEqual(stored["state"], "unknown")
        self.assertEqual(stored["code"], "channel-unknown")
        for name in pf.REQUIRED_CHANNELS:
            entry = (stored.get("channels") or {}).get(name) or {}
            self.assertFalse(entry.get("positive"))
        described = pf.describe_preflight(self.home)
        self.assertEqual(described["state"], "not-verified")
        self.assertEqual(described["code"], "no-preflight")
        with self.assertRaises(pf.PreflightError) as cm:
            pf.check_target_authority(self.home)
        self.assertEqual(cm.exception.code, "no-preflight")
        self.assertEqual(cm.exception.status, 404)

    def test_run_refuses_rerun_without_second_dispatch(self):
        calls = []
        pfid = self._run_verified(calls=calls)
        self.assertEqual(len(calls), 1)
        transport = _fake_transport(calls=calls)
        with self.assertRaises(pf.PreflightError) as cm:
            pf.run_preflight(self.home, pfid, transport=transport,
                             observer=_fixture_observer())
        self.assertEqual(cm.exception.code, "already-terminal")
        self.assertEqual(len(calls), 1)

    def test_run_rejects_unknown_pfid(self):
        with self.assertRaises(pf.PreflightError) as cm:
            pf.run_preflight(self.home, "pf-" + "0" * 16,
                             transport=_fake_transport(),
                             observer=_fixture_observer())
        self.assertEqual(cm.exception.status, 404)
        with self.assertRaises(pf.PreflightError) as cm2:
            pf.run_preflight(self.home, "nope",
                             transport=_fake_transport(),
                             observer=_fixture_observer())
        self.assertEqual(cm2.exception.status, 400)

    def test_double_run_second_busy_without_spawn(self):
        created = self._create("busy-world")
        token = pf._publish_claim(self.home, created["pfid"])
        self.addCleanup(pf._release_claim, self.home, created["pfid"],
                        token)
        calls = []
        with self.assertRaises(pf.PreflightError) as cm:
            pf.run_preflight(
                self.home, created["pfid"],
                transport=_fake_transport(calls=calls),
                observer=_fixture_observer())
        self.assertEqual(cm.exception.code, "busy")
        self.assertEqual(calls, [])

    def test_stale_claim_reaped_once(self):
        created = self._create("stale-world")
        token = pf._publish_claim(self.home, created["pfid"])
        self.addCleanup(pf._release_claim, self.home, created["pfid"],
                        token)
        doomed = os.path.join(self.home, "preflight",
                              created["pfid"] + ".claim.json")
        with open(doomed, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertTrue(pf._pid_alive(doc["pid"]))
        dead = 2 ** 30
        if pf._pid_alive(dead):
            self.skipTest("no dead pid available")
        doc["pid"] = dead
        with open(doomed, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        out = pf.run_preflight(
            self.home, created["pfid"],
            transport=_fake_transport(), observer=_fixture_observer())
        self.assertEqual(out["state"], "verified")

    def test_cancel_created_and_terminal_refusal(self):
        created = self._create("cancel-world")
        first = pf.cancel_preflight(self.home, created["pfid"])
        self.assertEqual(first["state"], "cancelled")
        self.assertTrue(pf._cancel_intent(
            self.home, created["pfid"]))
        raw_before = _read_leaf(self.home, created["pfid"], "outcome")
        with self.assertRaises(pf.PreflightError) as cm:
            pf.cancel_preflight(self.home, created["pfid"])
        self.assertEqual(cm.exception.code, "already-terminal")
        self.assertEqual(
            _read_leaf(self.home, created["pfid"], "outcome"),
            raw_before)

    def test_cancel_during_probe_wins(self):
        """Blocked transport + cancel mid-run: evidence kept, cancelled."""
        created = self._create("race-world")
        release = threading.Event()
        started = threading.Event()

        def _blocking(prompt, workdir, timeout, env, cancel):
            started.set()
            self.assertTrue(release.wait(timeout=30))
            return (0, "{}", {"requested_profile": dict(
                muse.SOURCE_REPAIR_PROFILE)})

        errors = []
        result = {}

        def _runner():
            try:
                result.update(pf.run_preflight(
                    self.home, created["pfid"], transport=_blocking,
                    observer=_default_all_unknown))
            except Exception as exc:  # noqa: BLE001 - collected
                errors.append(exc)

        thread = threading.Thread(target=_runner)
        thread.start()
        try:
            self.assertTrue(started.wait(timeout=30))
            cancelled = pf.cancel_preflight(
                self.home, created["pfid"])
            self.assertEqual(cancelled["state"], "cancelled")
            release.set()
            thread.join(timeout=60)
            self.assertFalse(thread.is_alive())
        finally:
            release.set()
            thread.join(timeout=60)
        self.assertEqual(errors, [])
        self.assertEqual(result.get("state"), "cancelled")
        status, _raw = pf._load_doc(
            self.home, created["pfid"], "status",
            pf.PREFLIGHT_STATUS_MAX_BYTES)
        self.assertEqual(status.get("state"), "cancelled")


def _default_all_unknown(home_path, pfid, inputs, spawn):
    return pf._default_observer(home_path, pfid, inputs, spawn)


def _read_leaf(home, pfid, kind):
    leaf = os.path.join(home, "preflight", f"{pfid}.{kind}.json")
    with open(leaf, "rb") as fh:
        return fh.read()


class PreflightEvaluatorCase(PreflightCase):
    def _inputs(self, config_status="established"):
        return {"policy": dict(pf.REQUIRED_POLICY),
                "config": {"status": config_status, "files": []}}

    def _auth(self, source="owned-fixture-observer", key=False):
        return {"meta_api_key_present": key,
                "credential_source": source,
                "billing_evidence": None,
                "observed_how": "fixture"}

    def test_evaluator_verified_shape(self):
        channels, auth = _fixture_observer()(
            None, None, None, None)
        state, reason, code = pf.evaluate_outcome(
            self._inputs(), channels, auth)
        self.assertEqual(state, "verified")
        self.assertEqual(code, "")

    def test_evaluator_allowed_channel_stays_unknown(self):
        """positive True + observed allowed is NOT a pass."""
        channels, auth = _fixture_observer()(
            None, None, None, None)
        channels["shell"] = {"required": "disabled",
                             "observed": "allowed", "positive": True,
                             "detail": "fabricated pass"}
        state, reason, code = pf.evaluate_outcome(
            self._inputs(), channels, auth)
        self.assertEqual(state, "unknown")
        self.assertEqual(code, "channel-unknown")

    def test_evaluator_refusals(self):
        channels, auth = _fixture_observer()(
            None, None, None, None)
        state, _r, code = pf.evaluate_outcome(
            {"policy": {"profile": "other"}}, channels, auth)
        self.assertEqual((state, code), ("refused", "bad-policy"))
        state, _r, code = pf.evaluate_outcome(
            self._inputs(config_status="unestablished"), channels, auth)
        self.assertEqual((state, code), ("refused",
                                         "config-unestablished"))
        state, _r, code = pf.evaluate_outcome(
            self._inputs(), channels, self._auth(key=True))
        self.assertEqual((state, code), ("refused", "api-key-present"))
        state, _r, code = pf.evaluate_outcome(
            self._inputs(), channels, self._auth(source="unknown"))
        self.assertEqual((state, code), ("refused",
                                         "auth-unestablished"))
        state, _r, code = pf.evaluate_outcome(
            self._inputs(), {}, auth)
        self.assertEqual(code, "channel-unknown")

    def test_observer_output_shape_validated(self):
        created = self._create("shape-world")

        def _bad_observer(home_path, pfid, inputs, spawn):
            return ({"shell": {"required": "disabled"}}, {
                "meta_api_key_present": False,
                "credential_source": "x", "billing_evidence": None,
                "observed_how": "bad"})

        out = pf.run_preflight(self.home, created["pfid"],
                               transport=_fake_transport(),
                               observer=_bad_observer)
        self.assertEqual(out["state"], "refused")
        self.assertEqual(out["code"], "probe-failed")
        outcome, _raw = pf._load_doc(
            self.home, created["pfid"], "outcome",
            pf.PREFLIGHT_OUTCOME_MAX_BYTES)
        self.assertEqual(outcome["state"], "refused")

    def test_first_create_establishes_owned_layout(self):
        # A genuinely fresh owned home has no preflight directory.
        # The explicit first create must establish the validated
        # layout itself (missing dir only) — never fail "missing"
        # on the home it was asked to initialize, and never create
        # anything on read-only describe/check paths.
        self.assertFalse(os.path.exists(
            os.path.join(self.home, "preflight")))
        described_before = pf.describe_preflight(self.home)
        self.assertEqual(described_before["state"], "absent")
        self.assertFalse(os.path.exists(
            os.path.join(self.home, "preflight")))
        created = self._create("first-world")
        self.assertFalse(created["reused"])
        self.assertTrue(os.path.isdir(
            os.path.join(self.home, "preflight")))
        described_after = pf.describe_preflight(self.home)
        self.assertEqual(described_after["state"], "not-verified")
        self.assertEqual(described_after["history"][0]["pfid"],
                         created["pfid"])

    def test_concurrent_same_key_create_publishes_once(self):
        # Four simultaneous public creates with one dedupe key:
        # exactly one identity is published; every loser either
        # reuses it or fails fast busy — never a second receipt.
        # No validator, identity, or fact patching.
        self._no_spawn()
        results = []
        errors = []

        def _once():
            try:
                results.append(
                    pf.create_preflight(self.home, "race-key"))
            except pf.PreflightError as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_once)
                   for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        for thread in threads:
            self.assertFalse(thread.is_alive())
        self.assertTrue(results)
        published = [r for r in results if not r.get("reused")]
        self.assertEqual(len(published), 1)
        for result in results:
            self.assertEqual(result["pfid"], published[0]["pfid"])
        for exc in errors:
            self.assertEqual(exc.code, "busy")
        self.assertEqual(len(results) + len(errors), 4)
        leaves = [name for name in os.listdir(
            os.path.join(self.home, "preflight"))
            if name.endswith(".input.json")]
        self.assertEqual(len(leaves), 1)

    def test_unreadable_record_stays_unreadable(self):
        # Fail-closed is kept: a record whose input file was replaced
        # by a directory must report 'unreadable', never a fabricated
        # result and never 'busy'. Deterministic: no perms, no spawn.
        self._no_spawn()
        created = self._create("unreadable-world")
        path = os.path.join(
            self.home, "preflight", created["pfid"] + ".input.json")
        os.remove(path)
        os.mkdir(path)
        with self.assertRaises(pf.PreflightError) as cm:
            pf.run_preflight(self.home, created["pfid"],
                             transport=_fake_transport(),
                             observer=_fixture_observer())
        self.assertEqual(cm.exception.code, "unreadable")

    def test_lock_symlink_refuses_without_follow(self):
        # A planted symlink at the run-lock leaf must refuse
        # (unreadable), never open/create outside the owned dir.
        # The suspicious leaf is left alone — not cleaned up.
        self._no_spawn()
        created = self._create("locklink-world")
        calls = []
        link = os.path.join(
            self.home, "preflight", created["pfid"] + ".lock")
        target = os.path.join(self.tmp.name, "outside-lock")
        os.symlink(target, link)
        try:
            with self.assertRaises(pf.PreflightError) as cm:
                pf.run_preflight(
                    self.home, created["pfid"],
                    transport=_fake_transport(calls=calls),
                    observer=_fixture_observer())
            self.assertEqual(cm.exception.code, "unreadable")
            self.assertEqual(calls, [])
            self.assertTrue(os.path.islink(link))
            self.assertFalse(os.path.exists(target))
            status, _raw = pf._load_doc(
                self.home, created["pfid"], "status",
                pf.PREFLIGHT_STATUS_MAX_BYTES)
            self.assertEqual(status["state"], "created")
        finally:
            if os.path.islink(link):
                os.unlink(link)

    def test_env_value_bound_completely(self):
        # Values bind COMPLETELY: a change visible only past the
        # old 4096 truncation prefix must still refuse as env-drift,
        # and an oversize value refuses at record time.
        prefix = "x" * 5000
        with mock.patch.dict(os.environ, {"PATH": prefix + "-A"}):
            values_a = pf._child_env_shape()["values"]
        with mock.patch.dict(os.environ, {"PATH": prefix + "-B"}):
            values_b = pf._child_env_shape()["values"]
        self.assertNotEqual(values_a.get("PATH"),
                            values_b.get("PATH"))
        with mock.patch.dict(
                os.environ,
                {"PATH": "x" * (pf._ENV_VALUE_MAX_CHARS + 1)}):
            with self.assertRaises(pf.PreflightError) as cm:
                pf._child_env_shape()
            self.assertEqual(cm.exception.code, "unreadable")
        # End-to-end drift: record under PATH-A, then check with
        # live PATH-B differing ONLY past the old 4096 truncation
        # prefix. Truncated binding would compare equal and miss
        # it; full binding refuses. Both recording and checking run
        # the real comparison — only current_identity is mocked.
        same_prefix = "y" * 5000
        recorded = json.loads(json.dumps(self.values))
        recorded["env"]["values"]["PATH"] = same_prefix
        live = json.loads(json.dumps(self.values))
        live["env"]["values"]["PATH"] = same_prefix + "-changed"
        with mock.patch(
                "openmainty.preflight.current_identity",
                return_value=recorded):
            self._no_spawn()
            created = pf.create_preflight(self.home, "envdrift-key")
            out = pf.run_preflight(
                self.home, created["pfid"],
                transport=_fake_transport(),
                observer=_fixture_observer())
            self.assertEqual(out["state"], "verified")
        with mock.patch(
                "openmainty.preflight.current_identity",
                return_value=live):
            with self.assertRaises(pf.PreflightError) as cm2:
                pf.check_target_authority(self.home)
            self.assertEqual(cm2.exception.code, "env-drift")

    def test_corrupt_cancel_flag_refuses_run(self):
        # A present-but-corrupt cancel flag is NOT absence: the run
        # refuses instead of spawning over possibly-tampered intent.
        self._no_spawn()
        created = self._create("cancel-corrupt-world")
        calls = []
        leaf = os.path.join(self.home, "preflight",
                            created["pfid"] + ".cancel.json")
        with open(leaf, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        with self.assertRaises(pf.PreflightError) as cm:
            pf._cancel_intent(self.home, created["pfid"])
        self.assertEqual(cm.exception.code, "corrupt")
        with self.assertRaises(pf.PreflightError) as cm2:
            pf.run_preflight(
                self.home, created["pfid"],
                transport=_fake_transport(calls=calls),
                observer=_fixture_observer())
        self.assertEqual(cm2.exception.code, "corrupt")
        self.assertEqual(calls, [])
        status, _raw = pf._load_doc(
            self.home, created["pfid"], "status",
            pf.PREFLIGHT_STATUS_MAX_BYTES)
        self.assertEqual(status["state"], "created")

    def test_leaf_identity_catches_rename_swap(self):
        # Name-to-inode continuity: after a rename swaps the live
        # name onto another inode, the previously opened fd must
        # NOT validate — closing fstat alone would wrongly accept
        # (it still describes the old valid inode).
        workdir = os.path.join(self.tmp.name, "nameswap")
        os.makedirs(workdir, mode=0o700)
        first = os.path.join(workdir, "a.txt")
        second = os.path.join(workdir, "b.txt")
        with open(first, "wb") as fh:
            fh.write(b"original-bytes")
        with open(second, "wb") as fh:
            fh.write(b"swapped-bytes!!")
        dfd = os.open(workdir, os.O_RDONLY | os.O_DIRECTORY
                      | os.O_NOFOLLOW)
        try:
            fd = os.open("a.txt", os.O_RDONLY | os.O_NOFOLLOW,
                         dir_fd=dfd)
            try:
                st = os.fstat(fd)
                os.replace(second, first)
                with self.assertRaises(pf.PreflightError) as cm:
                    pf._assert_leaf_identity(
                        dfd, "a.txt", fd, st, "test leaf")
                self.assertEqual(cm.exception.code, "corrupt")
            finally:
                os.close(fd)
            with open(first, "wb") as fh:
                fh.write(b"fresh-bytes-here")
            fd2 = os.open("a.txt", os.O_RDONLY | os.O_NOFOLLOW,
                          dir_fd=dfd)
            try:
                st2 = os.fstat(fd2)
                os.unlink(first)
                with self.assertRaises(pf.PreflightError) as cm2:
                    pf._assert_leaf_identity(
                        dfd, "a.txt", fd2, st2, "test leaf")
                self.assertEqual(cm2.exception.code, "unreadable")
            finally:
                os.close(fd2)
        finally:
            os.close(dfd)


class PreflightGateCase(PreflightCase):
    def test_check_refuses_without_receipt(self):
        with self.assertRaises(pf.PreflightError) as cm:
            pf.check_target_authority(self.home)
        self.assertEqual(cm.exception.status, 404)
        self.assertEqual(cm.exception.code, "no-preflight")

    def test_check_refuses_binary_drift(self):
        pfid = self._run_verified()
        drifted = dict(self.values)
        drifted = json.loads(json.dumps(self.values))
        drifted["binary"]["sha256"] = "00" * 32
        with mock.patch("openmainty.preflight.current_identity",
                        return_value=drifted):
            with self.assertRaises(pf.PreflightError) as cm:
                pf.check_target_authority(self.home)
        self.assertEqual(cm.exception.code, "binary-drift")
        drifted2 = json.loads(json.dumps(self.values))
        drifted2["binary"]["real_path"] = "/elsewhere/muse"
        with mock.patch("openmainty.preflight.current_identity",
                        return_value=drifted2):
            with self.assertRaises(pf.PreflightError) as cm2:
                pf.check_target_authority(self.home)
        self.assertEqual(cm2.exception.code, "binary-drift")

    def test_check_refuses_config_source_env_auth_drift(self):
        pfid = self._run_verified()
        cases = []
        changed = json.loads(json.dumps(self.values))
        changed["config"]["status"] = "unestablished"
        cases.append((changed, "config-unestablished"))
        changed = json.loads(json.dumps(self.values))
        changed["config"]["files"] = []
        cases.append((changed, "config-unestablished"))
        changed = json.loads(json.dumps(self.values))
        changed["source"]["files"][0]["sha256"] = "00" * 32
        cases.append((changed, "source-drift"))
        changed = json.loads(json.dumps(self.values))
        changed["source"]["app_version"] = "fx-2"
        cases.append((changed, "source-drift"))
        changed = json.loads(json.dumps(self.values))
        changed["env"]["passthrough_keys"] = ["PATH"]
        cases.append((changed, "env-drift"))
        changed = json.loads(json.dumps(self.values))
        changed["auth"]["credential_source"] = "other-observer"
        cases.append((changed, "auth-drift"))
        changed = json.loads(json.dumps(self.values))
        changed["auth"]["meta_api_key_present"] = True
        cases.append((changed, "api-key-present"))
        for live, code in cases:
            with mock.patch("openmainty.preflight.current_identity",
                            return_value=live):
                with self.assertRaises(pf.PreflightError) as cm:
                    pf.check_target_authority(self.home)
                self.assertEqual(cm.exception.code, code, live)

    def test_check_rejects_unknown_recorded_auth(self):
        created = self._create("unknown-auth")
        out = pf.run_preflight(
            self.home, created["pfid"],
            transport=_fake_transport(),
            observer=_fixture_observer(auth_source="unknown"))
        self.assertEqual(out["state"], "refused")
        self.assertEqual(out["code"], "auth-unestablished")

    def test_check_named_pfid_and_stale(self):
        first = self._run_verified(dedupe="first-world")
        second = self._run_verified(dedupe="second-world")
        ok = pf.check_target_authority(self.home, pfid=second)
        self.assertEqual(ok["pfid"], second)
        drifted = json.loads(json.dumps(self.values))
        drifted["binary"]["sha256"] = "11" * 32
        with mock.patch("openmainty.preflight.current_identity",
                        return_value=drifted):
            with self.assertRaises(pf.PreflightError):
                pf.check_target_authority(self.home, pfid=first)


class PreflightStorageCase(PreflightCase):
    def test_symlink_leaf_refuses(self):
        # Genuinely verified receipt FIRST, then its input leaf is
        # swapped for a symlink to an outside byte-identical copy.
        # The direct no-follow leaf reader refuses, and the gate
        # now truthfully reports top-level unreadable (502) for a
        # symlinked previously-verified input — never 404
        # no-preflight (which would misread tamper as absence),
        # never following the link, never healing or rewiring it.
        pfid = self._run_verified("link-world")
        leaf = os.path.join(self.home, "preflight", pfid + ".input.json")
        with open(leaf, "rb") as fh:
            raw = fh.read()
        os.remove(leaf)
        target = os.path.join(self.tmp.name, "real.json")
        with open(target, "wb") as fh:
            fh.write(raw)
        os.symlink(target, leaf)
        with self.assertRaises(pf.PreflightError) as cm:
            pf._load_doc(self.home, pfid, "input",
                         pf.PREFLIGHT_INPUT_MAX_BYTES)
        self.assertEqual(cm.exception.code, "unreadable")
        with self.assertRaises(pf.PreflightError) as cm2:
            pf.check_target_authority(self.home)
        self.assertEqual(cm2.exception.code, "unreadable")
        self.assertEqual(cm2.exception.status, 502)
        described = pf.describe_preflight(self.home)
        self.assertEqual(described["state"], "unreadable")
        self.assertEqual(described["code"], "unreadable")
        # Suspect never followed or repaired: the symlink stands and
        # the outside copy is byte-identical to the recorded input.
        self.assertTrue(os.path.islink(leaf))
        with open(target, "rb") as fh:
            self.assertEqual(fh.read(), raw)

    def test_dir_swap_refuses(self):
        pfid = self._run_verified(dedupe="swap-world")
        first_dir = os.path.join(self.home, "preflight")
        moved = os.path.join(self.tmp.name, "preflight-moved")
        os.rename(first_dir, moved)
        os.makedirs(first_dir, mode=0o700)
        try:
            with self.assertRaises(pf.PreflightError) as cm:
                pf.check_target_authority(self.home)
            self.assertEqual(cm.exception.code, "unreadable")
            # Swapped-but-present history reads as known-yet-unreadable,
            # never as legitimate absence: the directory exists, the
            # recorded latest pointer does not. The gate raises
            # unreadable (not 404 no-preflight) for the same reason.
            swapped_view = pf.describe_preflight(self.home)
            self.assertEqual(swapped_view["state"], "unreadable")
            self.assertEqual(swapped_view["code"], "unreadable")
            self.assertTrue(swapped_view["reason"])
            self.assertFalse(
                swapped_view["authority_now"]["authorized"])
        finally:
            import shutil
            shutil.rmtree(first_dir)
            os.rename(moved, first_dir)

    def test_truncated_scan_withholds_authority(self):
        # Overflow coverage: more receipts than the scan cap is an
        # arbitrary slice, never the newest slice. Create, check,
        # and describe must withhold (history-truncated), never
        # crown a stale receipt or claim absence.
        created = self._create("trunc-world")
        target = os.path.join(self.home, "preflight")
        for i in range(pf.PREFLIGHT_SCAN_CAP + 1):
            leaf = os.path.join(
                target, "pf-%016x.status.json" % (0x10000 + i))
            with open(leaf, "w", encoding="utf-8") as fh:
                fh.write("{}")
        with self.assertRaises(pf.PreflightError) as cm:
            pf.create_preflight(self.home, "trunc-second-key")
        self.assertEqual(cm.exception.code, "history-truncated")
        described = pf.describe_preflight(self.home)
        self.assertEqual(described["state"], "unknown")
        self.assertEqual(described["code"], "history-truncated")
        with self.assertRaises(pf.PreflightError) as cm2:
            pf.check_target_authority(self.home)
        self.assertEqual(cm2.exception.code, "history-truncated")
        self.assertEqual(cm2.exception.status, 502)

    def test_readable_unverified_stays_no_preflight(self):
        # Created-only history is readable but unverified: the gate
        # reports honest nothing-verified (404), never unreadable.
        created = self._create("created-only")
        described = pf.describe_preflight(self.home)
        self.assertEqual(described["state"], "not-verified")
        self.assertEqual(described["code"], "no-preflight")
        with self.assertRaises(pf.PreflightError) as cm:
            pf.check_target_authority(self.home)
        self.assertEqual(cm.exception.code, "no-preflight")
        self.assertEqual(cm.exception.status, 404)

    def test_tampered_outcome_refuses_unreadable(self):
        # A readable status with a present-but-invalid outcome is
        # tamper evidence, not a clean slate: fail closed.
        created = self._create("tamper-world")
        out = pf.run_preflight(
            self.home, created["pfid"],
            transport=_fake_transport(exit_code=0),
            observer=_fixture_observer(unknown_channels=True))
        self.assertEqual(out["state"], "unknown")
        leaf = os.path.join(self.home, "preflight",
                            created["pfid"] + ".outcome.json")
        with open(leaf, "w", encoding="utf-8") as fh:
            fh.write("{corrupt")
        described = pf.describe_preflight(self.home)
        self.assertEqual(described["state"], "unreadable")
        self.assertEqual(described["code"], "unreadable")
        with self.assertRaises(pf.PreflightError) as cm:
            pf.check_target_authority(self.home)
        self.assertEqual(cm.exception.code, "unreadable")
        self.assertEqual(cm.exception.status, 502)

    def test_fifo_leaf_refuses_without_hang(self):
        created = self._create("fifo-world")
        leaf = os.path.join(self.home, "preflight",
                            created["pfid"] + ".outcome.json")
        os.mkfifo(leaf)
        try:
            with self.assertRaises(pf.PreflightError) as cm:
                pf._load_doc(self.home, created["pfid"], "outcome",
                             pf.PREFLIGHT_OUTCOME_MAX_BYTES)
            self.assertEqual(cm.exception.code, "unreadable")
        finally:
            os.unlink(leaf)

    def test_no_mkdir_on_absent_layout(self):
        described = pf.describe_preflight(self.home)
        self.assertEqual(described["state"], "absent")
        self.assertFalse(os.path.exists(
            os.path.join(self.home, "preflight")))
        with self.assertRaises(pf.PreflightError) as cm:
            pf.check_target_authority(self.home)
        self.assertEqual(cm.exception.code, "no-preflight")
        self.assertFalse(os.path.exists(
            os.path.join(self.home, "preflight")))

    def test_duplicate_keys_and_schema_reject(self):
        created = self._create("dup-world")
        leaf = os.path.join(self.home, "preflight",
                            created["pfid"] + ".input.json")
        with open(leaf, "rb") as fh:
            raw = fh.read()
        doc = json.loads(raw.decode("utf-8"))
        doc["schema"] = 999
        with open(leaf, "wb") as fh:
            fh.write(json.dumps(doc).encode("utf-8"))
        with self.assertRaises(pf.PreflightError) as cm:
            pf._load_doc(self.home, created["pfid"], "input",
                         pf.PREFLIGHT_INPUT_MAX_BYTES)
        self.assertEqual(cm.exception.code, "corrupt")
        with open(leaf, "wb") as fh:
            fh.write(b'{"a": 1, "a": 2}')
        with self.assertRaises(pf.PreflightError) as cm2:
            pf._load_doc(self.home, created["pfid"], "input",
                         pf.PREFLIGHT_INPUT_MAX_BYTES)
        self.assertEqual(cm2.exception.code, "corrupt")

    def test_oversized_leaf_refuses(self):
        created = self._create("big-world")
        leaf = os.path.join(self.home, "preflight",
                            created["pfid"] + ".input.json")
        with open(leaf, "rb") as fh:
            raw = fh.read()
        with open(leaf, "wb") as fh:
            fh.write(raw + b" " * (pf.PREFLIGHT_INPUT_MAX_BYTES + 1))
        with self.assertRaises(pf.PreflightError) as cm:
            pf._load_doc(self.home, created["pfid"], "input",
                         pf.PREFLIGHT_INPUT_MAX_BYTES)
        self.assertEqual(cm.exception.code, "corrupt")

    def test_same_length_mutation_breaks_pin(self):
        # Same-length SCHEMA-VALID content change passes the closing
        # fstat (same dev/ino/size) and schema validation, but must
        # fail the recorded raw-byte SHA pin: the gate refuses
        # no-preflight instead of trusting the bytes.
        pfid = self._run_verified("same-world")
        leaf = os.path.join(self.home, "preflight", pfid + ".input.json")
        with open(leaf, "rb") as fh:
            raw = fh.read()
        doc = json.loads(raw.decode("utf-8"))
        old_version = doc["source"]["app_version"]
        self.assertIsInstance(old_version, str)
        new_version = ("Z" if not old_version.startswith("Z")
                       else "Y") + old_version[1:]
        self.assertEqual(len(new_version), len(old_version))
        self.assertNotEqual(new_version, old_version)
        mutated = json.loads(json.dumps(doc))
        mutated["source"]["app_version"] = new_version
        new_raw = pf._dumps_canonical(mutated)
        self.assertEqual(len(new_raw), len(raw))
        self.assertNotEqual(new_raw, raw)
        with open(leaf, "wb") as fh:
            fh.write(new_raw)
        # Still schema-valid: the pin (not the schema) must catch it.
        parsed, _ = pf._load_doc(self.home, pfid, "input",
                                 pf.PREFLIGHT_INPUT_MAX_BYTES)
        self.assertEqual(parsed["source"]["app_version"], new_version)
        with self.assertRaises(pf.PreflightError) as cm:
            pf.check_target_authority(self.home)
        self.assertEqual(cm.exception.code, "no-preflight")

    def test_nested_binary_config_shapes_reject(self):
        # Direct REAL validator oracles (no pin indirection): nested
        # binary/config tamper must refuse as corrupt even when the
        # top-level keys are intact.
        pfid = self._run_verified("shape-world")
        leaf = os.path.join(self.home, "preflight", pfid + ".input.json")
        with open(leaf, "rb") as fh:
            doc = json.loads(fh.read().decode("utf-8"))
        good = json.loads(json.dumps(doc))
        pf._validate_input_doc(good)
        cases = []
        bad = json.loads(json.dumps(good))
        bad["binary"]["sha256"] = "zz"
        cases.append(bad)
        bad = json.loads(json.dumps(good))
        bad["binary"] = {"status": "unavailable", "sha256": "ab" * 32,
                         "path": "", "real_path": ""}
        cases.append(bad)
        bad = json.loads(json.dumps(good))
        bad["binary"]["status"] = "maybe"
        cases.append(bad)
        bad = json.loads(json.dumps(good))
        bad["config"]["files"] = [{"path": "conf"}]
        cases.append(bad)
        bad = json.loads(json.dumps(good))
        bad["config"]["status"] = ""
        cases.append(bad)
        bad = json.loads(json.dumps(good))
        bad["config"]["extra"] = 1
        cases.append(bad)
        for i, malformed in enumerate(cases):
            with self.assertRaises(pf.PreflightError) as cm:
                pf._validate_input_doc(malformed)
            self.assertEqual(cm.exception.code, "corrupt", i)

    def test_validated_before_publish(self):
        verified = {"schema": 1, "pfid": "pf-" + "a" * 16,
                    "state": "verified", "inputs_sha256": "b" * 64,
                    "channels": {}, "auth": _fixture_values()["auth"],
                    "error": "", "code": "", "reason": "ok",
                    "ended_at": "2026-01-01T00:00:00+00:00",
                    "dir": [1, 2], "home": [3, 4]}
        with self.assertRaises(pf.PreflightError):
            pf._validate_outcome_doc(verified)
        empty_err = dict(verified)
        empty_err["channels"] = {
            n: {"required": pf.REQUIRED_DIRECTION[n],
                "observed": pf.REQUIRED_DIRECTION[n],
                "positive": True, "detail": "x"} for n in
            pf.REQUIRED_CHANNELS}
        pf._validate_outcome_doc(empty_err)
        bad_err = dict(empty_err)
        bad_err["error"] = "nonempty"
        with self.assertRaises(pf.PreflightError):
            pf._validate_outcome_doc(bad_err)
        cancelled = dict(empty_err)
        cancelled.update({"state": "cancelled", "channels": {},
                          "error": "r", "code": "cancelled"})
        pf._validate_outcome_doc(cancelled)
        cancelled_bad = dict(cancelled)
        cancelled_bad["channels"] = empty_err["channels"]
        with self.assertRaises(pf.PreflightError):
            pf._validate_outcome_doc(cancelled_bad)

    def test_history_newest_first(self):
        first = self._run_verified(dedupe="h1")
        second = self._run_verified(dedupe="h2")
        described = pf.describe_preflight(self.home)
        pfids = [h["pfid"] for h in described["history"]]
        self.assertEqual(pfids, sorted(pfids, reverse=True))
        self.assertIn(first, pfids)
        self.assertIn(second, pfids)

    def test_source_collector_reads_real_files(self):
        identity = pf._collect_source_identity()
        self.assertEqual(identity["status"], "ok")
        self.assertEqual(
            sorted(e["path"] for e in identity["files"]),
            sorted(pf._MANAGED_SOURCE_FILES))
        import pathlib
        pkg = pathlib.Path(pf.__file__).resolve().parent
        for entry in identity["files"]:
            raw = (pkg / entry["path"]).read_bytes()
            self.assertEqual(entry["sha256"],
                             hashlib.sha256(raw).hexdigest())

    def test_profiles_match_exactly(self):
        self.assertEqual(muse.SOURCE_REPAIR_PROFILE,
                         pf.REQUIRED_POLICY)

    def test_source_collector_output_satisfies_schema(self):
        identity = pf._collect_source_identity()
        pf._validate_source_shape(identity, "collector.source")
        # Genuine verified receipt FIRST: without one the gate
        # legitimately refuses no-preflight before any source
        # comparison, which would mask the drift proof.
        self._run_verified("drift-world")
        tampered = json.loads(json.dumps(identity))
        tampered["files"][0]["sha256"] = "00" * 32
        live = json.loads(json.dumps(self.values))
        live["source"] = tampered
        with mock.patch("openmainty.preflight.current_identity",
                        return_value=live):
            with self.assertRaises(pf.PreflightError) as cm:
                pf.check_target_authority(self.home)
            self.assertEqual(cm.exception.code, "source-drift")
            self.assertEqual(cm.exception.status, 409)


class MuseDedicatedCase(PreflightCase):
    def test_build_argv_exact(self):
        argv = muse.build_source_repair_cmd("do repair", "/w")
        self.assertEqual(argv[0:2], ["muse", "exec"])
        self.assertEqual(argv.count("--sandbox-network"), 1)
        self.assertIn("restricted",
                      argv[argv.index("--sandbox-network") + 1])
        for flag in ("--disable-shell", "--disable-web-tools",
                     "--disable-reminders",
                     "--no-foreign-personal-context",
                     "--disable-request-user-input",
                     "--approval-mode", "--approval-judge",
                     "--max-model-steps", "--max-tool-output-bytes"):
            self.assertIn(flag, argv)
        self.assertEqual(
            argv[argv.index("--max-model-steps") + 1], "25")
        self.assertEqual(
            argv[argv.index("--max-tool-output-bytes") + 1], "200000")
        self.assertEqual(argv[argv.index("--model") + 1],
                         "muse-spark-1.3")
        self.assertEqual(argv[-1], "do repair")
        with self.assertRaises(ValueError):
            muse.build_source_repair_cmd("x", "")
        with self.assertRaises(ValueError):
            muse.build_source_repair_cmd("x", "/w",
                                         output_schema="/etc/../x.json")
        with self.assertRaises(ValueError):
            muse.build_source_repair_cmd("", "/w")
        scoped = muse.build_source_repair_cmd(
            "x", "/w", output_schema="/w/out.json")
        self.assertIn("/w/out.json", scoped)

    def test_fingerprint_full_identity(self):
        base = _fixture_values()
        full = muse.source_repair_fingerprint(base)
        self.assertEqual(len(full), 64)
        for mutate in ("binary.sha256", "binary.real_path",
                       "config.files", "source.app_version",
                       "source.files", "env.passthrough_keys",
                       "auth.credential_source",
                       "auth.meta_api_key_present",
                       "auth.contributor_env_present"):
            other = json.loads(json.dumps(base))
            node = other
            parts = mutate.split(".")
            for part in parts[:-1]:
                node = node[part]
            leaf = parts[-1]
            if isinstance(node[leaf], bool):
                node[leaf] = not node[leaf]
            elif isinstance(node[leaf], list):
                node[leaf] = node[leaf] + ["zzz"]
            else:
                node[leaf] = "CHANGED"
            self.assertNotEqual(
                muse.source_repair_fingerprint(other), full, mutate)
        with self.assertRaises(ValueError):
            muse.source_repair_fingerprint(None)

    def test_resolve_pinned_identities(self):
        real = os.path.join(self.tmp.name, "muse-bin")
        with open(real, "wb") as fh:
            fh.write(b"fake-binary-bytes")
        with self.assertRaises(OSError) as cm:
            muse.resolve_pinned_muse(real)
        self.assertIn("drift", str(cm.exception))
        link = os.path.join(self.tmp.name, "muse-link")
        os.symlink(real, link)
        with self.assertRaises(OSError) as cm2:
            muse.resolve_pinned_muse(link)
        self.assertIn("symlink", str(cm2.exception))
        with self.assertRaises(OSError):
            muse.resolve_pinned_muse(
                os.path.join(self.tmp.name, "missing"))
        # The fixture setUp configures a binary; unset it to prove the
        # genuinely unconfigured path refuses.
        saved_bin = os.environ.pop("OPENMAINTY_MUSE_BINARY", None)
        try:
            with self.assertRaises(OSError) as cm3:
                muse.resolve_pinned_muse()
        finally:
            if saved_bin is not None:
                os.environ["OPENMAINTY_MUSE_BINARY"] = saved_bin
        self.assertIn("OPENMAINTY_MUSE_BINARY", str(cm3.exception))
        first = muse._snapshot_binary(real)
        second = muse._snapshot_binary(real)
        self.assertTrue(muse._same_binary_snapshot(first, second))
        with open(real, "ab") as fh:
            fh.write(b"more")
        third = muse._snapshot_binary(real)
        self.assertFalse(muse._same_binary_snapshot(first, third))

    def test_dedicated_env_narrow(self):
        prev = os.environ.get("META_API_KEY")
        os.environ["META_API_KEY"] = "secret-value"
        try:
            env = muse._dedicated_child_env("/w")
            self.assertNotIn("META_API_KEY", env)
            for key in ("PATH", "HOME"):
                if key in os.environ:
                    self.assertEqual(env.get(key), os.environ[key])
            # The owned toolchain-cache contract (not a magic
            # single key): cache entries equal worker_cache_env,
            # and every path value roots at the workdir.
            self.assertNotIn("MUSE_TRANSPORT_TRACE", env)
            expected_cache = muse.worker_cache_env("/w")
            self.assertTrue(expected_cache)
            for key, value in expected_cache.items():
                self.assertEqual(env.get(key), value)
                if os.sep in str(value):
                    self.assertTrue(
                        str(value).startswith("/w" + os.sep), key)
            traced = muse._dedicated_child_env(
                "/w", {"OPENMAINTY_PREFLIGHT_PROBE": "pf-x"}, trace=True)
            self.assertEqual(traced["MUSE_TRANSPORT_TRACE"], "1")
            self.assertEqual(
                traced["OPENMAINTY_PREFLIGHT_PROBE"], "pf-x")
        finally:
            if prev is None:
                os.environ.pop("META_API_KEY", None)
            else:
                os.environ["META_API_KEY"] = prev

    def test_exec_requires_admission_kind(self):
        self._no_spawn()
        code, _out, usage = muse.exec_source_repair(
            "x", "/w", admission_kind=None)
        self.assertEqual(code, 125)
        self.assertEqual(usage.get("code"), "no-authority")

    def test_exec_trace_target_refused(self):
        self._no_spawn()
        pfid = self._run_verified()
        code, _out, usage = muse.exec_source_repair(
            "x", "/w", admission_kind="target", trace=True,
            preflight_home=self.home, preflight_pfid=pfid)
        self.assertEqual(code, 125)

    def test_exec_stage0_unproved_no_spawn(self):
        self._no_spawn()
        code, _out, usage = muse.exec_source_repair(
            "x", "/w", admission_kind="stage0",
            preflight_home=self.home)
        self.assertEqual(code, 125)
        self.assertEqual(usage.get("code"), "native-containment-unproved")

    def test_exec_target_no_receipt(self):
        self._no_spawn()
        code, _out, usage = muse.exec_source_repair(
            "x", "/w", admission_kind="target",
            preflight_home=self.home)
        self.assertEqual(code, 125)
        self.assertEqual(usage.get("code"), "no-preflight")

    def test_exec_target_verified_still_no_spawn(self):
        """Authority passes, but native containment is unproved."""
        self._no_spawn()
        pfid = self._run_verified()
        code, _out, usage = muse.exec_source_repair(
            "x", "/w", admission_kind="target",
            preflight_home=self.home, preflight_pfid=pfid)
        self.assertEqual(code, 125)
        self.assertEqual(usage.get("code"),
                         "native-containment-unproved")

    def test_exec_api_key_presence_refuses(self):
        # Ambient hygiene fires BEFORE authority I/O and resolve, so
        # no resolve/snapshot mocks are needed: the real env path
        # refuses on the empty-valued key alone.
        self._no_spawn()
        prev = os.environ.get("META_API_KEY")
        os.environ["META_API_KEY"] = ""
        try:
            pfid = self._run_verified()
            code, _out, usage = muse.exec_source_repair(
                "x", self.tmp.name, admission_kind="target",
                preflight_home=self.home,
                preflight_pfid=pfid)
        finally:
            if prev is None:
                os.environ.pop("META_API_KEY", None)
            else:
                os.environ["META_API_KEY"] = prev
        self.assertEqual(code, 125)
        self.assertEqual(usage.get("code"), "api-key-present")

    def test_communicate_bounded_ok_and_timeout(self):
        # The ONLY test allowed to spawn: fresh OWNED interpreter
        # children (never a model/CLI), cwd inside the owned tmp
        # root, scrubbed inherited env, own process group, bounded
        # cleanup on ANY assertion failure.
        scrubbed = {"PATH": os.environ.get("PATH", ""),
                    "HOME": self.tmp.name}
        procs = []
        self.addCleanup(lambda: [muse._kill_owned_group(p.pid)
                                 for p in procs if p.poll() is None])
        self.addCleanup(lambda: [p.wait(timeout=30) for p in procs])
        proc = subprocess.Popen(
            [sys.executable, "-c", "print('hi')"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, cwd=self.tmp.name,
            env=scrubbed, start_new_session=True)
        procs.append(proc)
        out, err, outcome, oc, ec, joined, reaped = \
            muse._communicate_bounded(proc, 30, None, 1 << 20)
        self.assertEqual(outcome, "ok")
        self.assertEqual(out, b"hi\n")
        self.assertTrue(joined)
        self.assertTrue(reaped)
        before = len(threading.enumerate())
        slow = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, cwd=self.tmp.name,
            env=scrubbed, start_new_session=True)
        procs.append(slow)
        out, err, outcome, oc, ec, joined, reaped = \
            muse._communicate_bounded(slow, 2, None, 1 << 20)
        self.assertEqual(outcome, "timeout")
        self.assertTrue(joined)
        self.assertTrue(reaped)
        self.assertIsNotNone(slow.poll())
        self.assertEqual(len(threading.enumerate()), before)

    def test_communicate_overflow_stops_child(self):
        # Owned pipe, over the cap: outcome overflow with the kept
        # prefix only, child stopped promptly (no draining to the
        # long leader timeout), group reaped.
        scrubbed = {"PATH": os.environ.get("PATH", ""),
                    "HOME": self.tmp.name}
        procs = []
        self.addCleanup(lambda: [muse._kill_owned_group(p.pid)
                                 for p in procs if p.poll() is None])
        self.addCleanup(lambda: [p.wait(timeout=30) for p in procs])
        proc = subprocess.Popen(
            [sys.executable, "-c",
             "import sys; sys.stdout.write('x' * 200000)"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, cwd=self.tmp.name,
            env=scrubbed, start_new_session=True)
        procs.append(proc)
        out, err, outcome, oc, ec, joined, reaped = \
            muse._communicate_bounded(proc, 30, None, 1000)
        self.assertEqual(outcome, "overflow")
        self.assertTrue(oc)
        self.assertEqual(out, b"x" * 1000)
        self.assertTrue(joined)
        self.assertTrue(reaped)
        self.assertIsNotNone(proc.poll())

    def test_communicate_cancel_kills_group(self):
        # Mid-run cancel: the owned group dies promptly, readers
        # join, nothing is trusted.
        scrubbed = {"PATH": os.environ.get("PATH", ""),
                    "HOME": self.tmp.name}
        procs = []
        self.addCleanup(lambda: [muse._kill_owned_group(p.pid)
                                 for p in procs if p.poll() is None])
        self.addCleanup(lambda: [p.wait(timeout=30) for p in procs])
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, cwd=self.tmp.name,
            env=scrubbed, start_new_session=True)
        procs.append(proc)
        cancel = threading.Event()
        setter = threading.Timer(0.5, cancel.set)
        setter.start()
        try:
            out, err, outcome, oc, ec, joined, reaped = \
                muse._communicate_bounded(proc, 30, cancel, 1 << 20)
        finally:
            setter.join(timeout=30)
            setter.cancel()
        self.assertEqual(outcome, "cancelled")
        self.assertEqual(out, b"")
        self.assertTrue(joined)
        self.assertTrue(reaped)
        self.assertIsNotNone(proc.poll())

    def test_communicate_read_error(self):
        # A stream that fails mid-read (closed fd on a real owned
        # pipe) refuses distinctly; the other stream's EOF alone
        # must not read as a clean result.
        r_out, w_out = os.pipe()
        os.write(w_out, b"kept-partial")
        os.close(w_out)
        f_out = os.fdopen(r_out, "rb")
        r_err, w_err = os.pipe()
        os.close(w_err)
        f_err = os.fdopen(r_err, "rb")
        f_out.close()
        try:
            class _FakeProc(object):
                pid = 1 << 30
                returncode = 0

                def poll(self):
                    return 0

                def wait(self, timeout=None):
                    return 0

            proc = _FakeProc()
            proc.stdout = f_out
            proc.stderr = f_err
            # The fabricated pid must NEVER reach a real killpg:
            # the signal sink is mocked. The fake leader already
            # reports poll 0, so the loop is skipped, both threads
            # join, and read-error is assigned after cleanup — no
            # signal path is taken, so the sink must stay silent.
            with mock.patch.object(
                    muse, "_kill_owned_group",
                    return_value=True) as kill_sink:
                out, err, outcome, oc, ec, joined, reaped = \
                    muse._communicate_bounded(proc, 30, None, 1 << 20)
            kill_sink.assert_not_called()
            self.assertEqual(outcome, "read-error")
            self.assertEqual(err, b"")
            self.assertTrue(joined)
            self.assertTrue(reaped)
        finally:
            try:
                f_err.close()
            except Exception:
                pass

    def test_communicate_incomplete_ok_reports_unjoined(self):
        # Faithful incomplete-EOF telemetry with NO detached
        # descendant: both pipe ends are parent-owned. The leader
        # "exited" (fake poll) while the test still holds the
        # stdout write end open, so the stdout reader cannot reach
        # EOF through the whole bounded join budget: outcome stays
        # "ok" but joined_ok is False. BufferedReader.read(65536)
        # does NOT publish the 12 held bytes without EOF, so the
        # capture may be empty — assert it is an honest
        # prefix/subset (never an EOF-complete result) and that the
        # caller treats it as untrusted/incomplete. The helper's
        # unjoined branch signals the owned group again for
        # cleanup: the mocked sink must record exactly the fake
        # pid (never a real signal). (The prior grandchild draft
        # held NEITHER pipe — DEVNULL on both — so it could never
        # keep a reader from EOF; this replaces it, it does not
        # invert the assertion.)
        before = set(threading.enumerate())
        r_out, w_out = os.pipe()
        os.write(w_out, b"held-partial")
        f_out = os.fdopen(r_out, "rb")
        r_err, w_err = os.pipe()
        os.close(w_err)
        f_err = os.fdopen(r_err, "rb")

        class _FakeProc(object):
            pid = 424242
            returncode = 0

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

        proc = _FakeProc()
        proc.stdout = f_out
        proc.stderr = f_err
        try:
            with mock.patch.object(
                    muse, "_kill_owned_group") as kill_sink:
                out, err, outcome, oc, ec, joined, reaped = \
                    muse._communicate_bounded(proc, 1, None, 1 << 20)
                self.assertEqual(outcome, "ok")
                self.assertTrue(b"held-partial".startswith(out))
                self.assertEqual(err, b"")
                self.assertFalse(joined)
                self.assertTrue(reaped)
                for call in kill_sink.call_args_list:
                    self.assertEqual(call, mock.call(424242))
                self.assertTrue(kill_sink.call_args_list)
        finally:
            # Release the held write end: EOF lets the stranded
            # reader finish. Re-join on a bounded deadline and
            # prove no residual readers, then close the read
            # streams ONLY after their readers are dead (never a
            # close under a live reader).
            try:
                os.close(w_out)
            except OSError:
                pass
            probe = threading.Event()
            for _ in range(200):
                residual = [t for t in threading.enumerate()
                            if t not in before and t.is_alive()]
                if not residual:
                    break
                probe.wait(0.05)
            self.assertEqual(
                [t for t in threading.enumerate()
                 if t not in before and t.is_alive()], [])
            try:
                f_out.close()
            except Exception:
                pass
            try:
                f_err.close()
            except Exception:
                pass

    def test_budgets_validate_before_spawn(self):
        # Invalid budgets refuse with ValueError from the shared
        # pre-Popen gate (exec calls it before Popen, so a bad
        # budget can never leak a spawned child).
        for bad_timeout in (0, -1, float("inf"), float("nan"),
                            "30", True, None):
            with self.assertRaises(ValueError):
                muse._check_bounded_budgets(bad_timeout, 1 << 20)
        for bad_max in (0, -5, "x", True, None):
            with self.assertRaises(ValueError):
                muse._check_bounded_budgets(30, bad_max)
        self.assertIsNone(muse._check_bounded_budgets(30, 1 << 20))

    def _start_exec_caller_fixtures(self, capture):
        """Fixture ONLY for reaching the REAL exec capture-trust
        gate offline. Mocked legs (labelled, never operational
        proof): scripted admission decision (check_target_authority
        + native_containment), owned fixture binary identity
        (resolve + re-snapshot), the capture tuple itself, the
        spawn (Popen returns an owned fake proc — no real child),
        and the signal sink. Production authority, containment,
        binary, and spawn stay unproved/refusing. Only the
        CALLER trust decision — refuse unjoined/unreaped "ok",
        validate budgets pre-Popen — is real product behavior.
        Returns (popen_mock, kill_mock) for exact-call assertions.
        """

        class _FakeProc(object):
            pid = 424243
            returncode = 0

            def wait(self, timeout=None):
                return 0

        snap = {"path": "/owned/fixture/muse",
                "real_path": "/owned/fixture/muse",
                "sha256": "f" * 64, "dev": 1, "ino": 2, "size": 3}
        named = [
            ("admission",
             mock.patch.object(
                 pf, "check_target_authority",
                 return_value={"authorized": True,
                               "kind": "target"})),
            ("containment",
             mock.patch.object(pf, "native_containment",
                               return_value=None)),
            ("resolve",
             mock.patch.object(muse, "resolve_pinned_muse",
                               return_value=dict(snap))),
            ("resnapshot",
             mock.patch.object(muse, "_snapshot_binary",
                               return_value=dict(snap))),
            ("capture",
             mock.patch.object(muse, "_communicate_bounded",
                               return_value=capture)),
            ("spawn",
             mock.patch.object(muse.subprocess, "Popen",
                               return_value=_FakeProc())),
            ("sink",
             mock.patch.object(muse, "_kill_owned_group",
                               return_value=True)),
        ]
        started = {}
        for _name, patcher in named:
            # Start then register the PATCHER stop immediately:
            # .start() returns the mock value, and stopping THAT
            # would leave the global (e.g. subprocess.Popen)
            # patched for all following methods. Immediate
            # registration also restores correctly if a later
            # start fails partway through.
            started[_name] = patcher.start()
            self.addCleanup(patcher.stop)
        return started["spawn"], started["sink"]

    def test_exec_refuses_unjoined_ok_capture(self):
        # Real caller, mocked capture outcome=ok + joined=False:
        # must refuse nonzero with no trusted output, report, or
        # trace. Byte COUNTS are measured telemetry; raw bytes
        # never return and no exit/report fields are set.
        popen_mock, kill_mock = self._start_exec_caller_fixtures(
            (b"partial", b"", "ok", False, False, False, True))
        with mock.patch.dict(os.environ):
            os.environ.pop("META_API_KEY", None)
            code, out, usage = muse.exec_source_repair(
                "repair prompt", self.tmp.name,
                admission_kind="target", preflight_home=self.home)
        popen_mock.assert_called_once()
        self.assertEqual(
            popen_mock.call_args[0][0][0], "/owned/fixture/muse")
        kill_mock.assert_called_once_with(424243)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("incomplete", usage.get("error", ""))
        self.assertEqual(
            set(usage) <= {"stdout_bytes", "stderr_bytes",
                           "stdout_truncated", "stderr_truncated",
                           "readers_joined", "child_reaped",
                           "tokens_measurable", "tokens", "error"},
            True)
        self.assertNotIn("exit_code", usage)

    def test_exec_refuses_unreaped_ok_capture(self):
        # Same caller gate, other half: joined but never reaped.
        popen_mock, kill_mock = self._start_exec_caller_fixtures(
            (b"partial", b"", "ok", False, False, True, False))
        with mock.patch.dict(os.environ):
            os.environ.pop("META_API_KEY", None)
            code, out, usage = muse.exec_source_repair(
                "repair prompt", self.tmp.name,
                admission_kind="target", preflight_home=self.home)
        popen_mock.assert_called_once()
        kill_mock.assert_called_once_with(424243)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("incomplete", usage.get("error", ""))
        self.assertNotIn("exit_code", usage)

    def test_exec_budget_invalid_no_spawn(self):
        # The REAL caller validates budgets before Popen: an
        # infinite timeout refuses 125 and the spawn sentinel is
        # never touched — even with fixture admission/binary.
        popen_mock, _kill_mock = self._start_exec_caller_fixtures(
            (b"", b"", "ok", False, False, True, True))
        with mock.patch.dict(os.environ):
            os.environ.pop("META_API_KEY", None)
            code, _out, usage = muse.exec_source_repair(
                "x", self.tmp.name, timeout=float("inf"),
                admission_kind="target", preflight_home=self.home)
        self.assertEqual(code, 125)
        self.assertIn("refused", usage.get("error", ""))
        popen_mock.assert_not_called()

    def test_run_route_without_receipt_refuses(self):
        self._no_spawn()
        gate = mock.patch.object(
            serve.chat, "subscription_error", return_value=None)
        gate.start()
        self.addCleanup(gate.stop)
        with self.assertRaises(serve.ApiError) as cm:
            serve.api_derived_fix_preflight_run(
                {"pfid": "pf-" + "0" * 16, "confirm": True})
        self.assertEqual(cm.exception.status, 404)


class PreflightServeCase(PreflightCase):
    def _loopback(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        thread = threading.Thread(target=server.serve_forever,
                                  daemon=True)
        thread.start()
        # addCleanup runs LIFO, so register in reverse execution
        # order: shutdown and close FIRST (unblock serve_forever),
        # then the bounded join, then the zero-residual assertion.
        # The old order joined a still-serving thread first and
        # burned ~30s per HTTP test. Cleanup still runs on
        # assertion failure (finally-safe via addCleanup).
        def _assert_dead():
            self.assertFalse(thread.is_alive())

        self.addCleanup(_assert_dead)
        self.addCleanup(thread.join, 30)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def _get(self, server, path):
        port = server.server_address[1]
        with urlrequest.urlopen(
                f"http://127.0.0.1:{port}{path}",
                timeout=15) as resp:
            return resp.status, json.loads(
                resp.read().decode("utf-8"))

    def test_status_envelope_absent(self):
        server = self._loopback()
        status, body = self._get(
            server, "/api/integration/derived-fix/preflight")
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "absent")
        self.assertIn("authority_now", body)
        self.assertFalse(
            body["authority_now"].get("authorized"))

    def test_status_envelope_verified(self):
        pfid = self._run_verified()
        server = self._loopback()
        status, body = self._get(
            server, "/api/integration/derived-fix/preflight")
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "verified")
        self.assertEqual(body["pfid"], pfid)
        self.assertTrue(body["authority_now"].get("authorized"))
        self.assertEqual(
            body["authority_now"].get("pfid"), pfid)
        self.assertEqual(
            [h["pfid"] for h in body["history"]],
            sorted([h["pfid"] for h in body["history"]],
                   reverse=True))

    def test_create_cancel_lifecycle(self):
        # Create/cancel spend no quota: the routes take no
        # subscription gate, only the run route does.
        self._no_spawn()
        created = serve.api_derived_fix_preflight_create(
            {"confirm": True})
        self.assertFalse(created.get("reused"))
        pfid = created["pfid"]
        cancelled = serve.api_derived_fix_preflight_cancel(
            {"pfid": pfid, "confirm": True})
        self.assertEqual(cancelled["state"], "cancelled")
        with self.assertRaises(serve.ApiError) as cm:
            serve.api_derived_fix_preflight_cancel(
                {"pfid": pfid, "confirm": True})
        self.assertEqual(cm.exception.status, 409)
        described = serve.api_derived_fix_preflight({})
        self.assertEqual(described["state"], "not-verified")
        states = {h["pfid"]: h["state"]
                  for h in described["history"]}
        self.assertEqual(states.get(pfid), "cancelled")

    def test_run_route_releases_lease_on_refusal(self):
        self._no_spawn()
        gate = mock.patch.object(
            serve.chat, "subscription_error", return_value=None)
        gate.start()
        self.addCleanup(gate.stop)
        with self.assertRaises(serve.ApiError) as cm:
            serve.api_derived_fix_preflight_run(
                {"pfid": "pf-" + "0" * 16, "confirm": True})
        self.assertEqual(cm.exception.status, 404)
        # The refused run holds no lease: a probe acquires cleanly.
        token = sched.acquire_worker_lease("post-refusal-probe")
        try:
            self.assertIsNotNone(token)
        finally:
            if token is not None:
                sched.release_worker_lease(token)

    def test_create_rejects_without_confirm(self):
        server = self._loopback()
        port = server.server_address[1]
        data = json.dumps({"confirm": False}).encode("utf-8")
        req = urlrequest.Request(
            f"http://127.0.0.1:{port}"
            "/api/integration/derived-fix/preflight/create",
            data=data, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urlrequest.urlopen(req, timeout=15) as resp:
                status, body = (resp.status, json.loads(
                    resp.read().decode("utf-8")))
        except urlrequest.HTTPError as exc:
            status, body = (exc.code, json.loads(
                exc.read().decode("utf-8")))
        self.assertEqual(status, 400)
        self.assertIn("error", body)
        self.assertNotIn("code", body)


# ---------------------------------------------------------------------------
# App-owned MSP host factory diagnostics (offline, IO-boundary seams).
#
# The ONLY patched seams are muse._msp_resolve_snapshot (fixture
# binary identity over a REAL temp file, so same-inode and
# inode-replacement drift are genuine), muse._msp_build_wrapper
# (fixture argv + a REAL policy file identity, revalidated live by
# production code; one test uses the real ncontain builder), and
# muse._msp_spawn_process (fake child backed by REAL os.pipes: real
# blocking reads, real nonblocking stdin, real EOF). Parsers,
# envelope checks, registry, ownership, redaction, and cleanup
# accounting under test are always live. No network, no real home,
# no target repos, no model/session/login/trace, and no worker
# admission is ever constructed here.
# ---------------------------------------------------------------------------


def _msp_test_line(obj):
    return (json.dumps(obj, sort_keys=True, separators=(",", ":"))
            + "\n").encode("utf-8")


def _msp_test_ok(ident, result):
    return _msp_test_line(
        {"jsonrpc": "2.0", "id": ident, "result": result})


# Reviewed pinned schema fingerprint (literal: the fixture pins the
# reviewed value instead of echoing the production constant, so a
# production pin change fails loudly here).
_MSP_PINNED_FP = ("sha256:c94c3d1f2f51e6424074b9ec43eac"
                  "3eedcf532d3dcd77351ec1383a87d3049c6")


def _msp_test_init_result(muse_home, **overrides):
    doc = {"serverInfo": {"name": "muse", "version": "1.4.14"},
           "schema": {"version": 1,
                      "fingerprint": _MSP_PINNED_FP},
           "museHome": muse_home,
           "sessionDurability": "ephemeral",
           "sandboxAvailability": "unavailable",
           "grantedCapabilities": [],
           "experimentalApi": True}
    doc.update(overrides)
    return doc


class _MspScriptedChild:
    """Fake Popen: responder thread on real os.pipes (no child).

    A responder thread reads each factory request line from the
    real stdin pipe, records raw + parsed bytes, optionally waits,
    then writes the scripted raw response lines for that method.
    Responses therefore always arrive AFTER their request was sent
    (like a real host), so pending/seen accounting is exercised
    deterministically — never a preload race. Unscripted methods
    get no response (the waiter times out). stdin EOF (close) ends
    the responder, which closes the stdout write end so readers
    reach real EOF. pid is an impossible value so the real signal
    sink can never touch a live process; tests that assert
    signalling patch the sink.
    """
    FAKE_PID = 1 << 30

    def __init__(self, script=None, stderr_bytes=b"",
                 exit_code=0, wait_mode="code", delay=0.0):
        out_r, out_w = os.pipe()
        in_r, in_w = os.pipe()
        # UNBUFFERED binary FileIO (like Popen bufsize=0): a
        # BufferedReader.read(65536) would block for a full buffer
        # or EOF while the responder still awaits requests.
        self.stdout = os.fdopen(out_r, "rb", buffering=0)
        self.stdin = os.fdopen(in_w, "wb", buffering=0)
        self._in_r = in_r
        self._out_w = out_w
        err_r, err_w = os.pipe()
        if stderr_bytes:
            os.write(err_w, stderr_bytes)
        os.close(err_w)
        self.stderr = os.fdopen(err_r, "rb", buffering=0)
        self.pid = self.FAKE_PID
        self.returncode = None
        self.alive = True
        self._finished = threading.Event()
        self._exit_code = exit_code
        self._wait_mode = wait_mode
        self.wait_calls = []
        self._script = dict(script or {})
        self._delay = delay
        self.requests = []
        self.raw_requests = []
        self._responder = threading.Thread(
            target=self._respond, daemon=True)
        self._responder.start()

    def _respond(self):
        buf = bytearray()
        try:
            while True:
                try:
                    chunk = os.read(self._in_r, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                while True:
                    end = buf.find(b"\n")
                    if end < 0:
                        break
                    line = bytes(buf[:end])
                    del buf[:end + 1]
                    if not line:
                        continue
                    self.raw_requests.append(line)
                    try:
                        obj = json.loads(line.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        continue
                    self.requests.append(obj)
                    if self._delay:
                        time.sleep(self._delay)
                    payloads = self._script.get(
                        obj.get("method"), [])
                    if isinstance(payloads, bytes):
                        payloads = [payloads]
                    for payload in payloads:
                        try:
                            os.write(self._out_w, payload)
                        except OSError:
                            break
        finally:
            for fd in (self._out_w, self._in_r):
                try:
                    os.close(fd)
                except OSError:
                    pass
            if self._wait_mode != "none":
                # A reaped child reports its code and stops being
                # alive. Stubborn ("none") mode never finishes on
                # its own: poll keeps reporting running and wait
                # keeps reporting unreaped, like a real stuck child.
                self.returncode = self._exit_code
                self.alive = False
                self._finished.set()

    def poll(self):
        if self._finished.is_set():
            return self.returncode
        return None if self.alive else 0

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self._wait_mode == "none":
            return None
        self._finished.wait(timeout=timeout)
        if self._finished.is_set():
            return self.returncode
        return None

    def join_responder(self, timeout=15):
        self._responder.join(timeout=timeout)
        return not self._responder.is_alive()

    def force_close(self):
        try:
            self.stdin.close()
        except (OSError, ValueError):
            pass
        self.join_responder(timeout=15)
        for stream in ("stdout", "stderr"):
            try:
                getattr(self, stream).close()
            except (OSError, ValueError):
                pass


class MspHostFactoryCase(unittest.TestCase):
    def setUp(self):
        self._real_wrapper = muse._msp_build_wrapper
        self._real_resolve = muse._msp_resolve_snapshot
        base = tempfile.mkdtemp(prefix="msp-case-")
        self.work = os.path.realpath(base)
        os.chmod(self.work, 0o700)
        self.addCleanup(shutil.rmtree, self.work, True)
        root = tempfile.mkdtemp(prefix="msp-root-")
        self.root = os.path.realpath(root)
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        prev = os.environ.pop("META_API_KEY", None)

        def _restore():
            if prev is None:
                os.environ.pop("META_API_KEY", None)
            else:
                os.environ["META_API_KEY"] = prev

        self.addCleanup(_restore)
        self.bin_path = os.path.join(self.work, "muse-bin")
        with open(self.bin_path, "wb") as fh:
            fh.write(b"owned-fixture-binary-bytes")
        os.chmod(self.bin_path, 0o700)
        bst = os.stat(self.bin_path)
        self.snap = {"path": self.bin_path,
                     "real_path": self.bin_path,
                     "sha256": hashlib.sha256(
                         b"owned-fixture-binary-bytes").hexdigest(),
                     "dev": bst.st_dev, "ino": bst.st_ino,
                     "size": bst.st_size}
        self.policy_path = os.path.join(self.work, "POLICY.sb")
        with open(self.policy_path, "wb") as fh:
            fh.write(b"fixture-policy-bytes")
        pst = os.stat(self.policy_path)
        self.policy = {"path": self.policy_path,
                       "sha256": hashlib.sha256(
                           b"fixture-policy-bytes").hexdigest(),
                       "size": pst.st_size, "dev": pst.st_dev,
                       "ino": pst.st_ino}
        self.spawned = []
        for name in ("_msp_resolve_snapshot", "_msp_build_wrapper"):
            if name == "_msp_resolve_snapshot":
                patcher = mock.patch.object(
                    muse, name, return_value=dict(self.snap))
            else:
                patcher = mock.patch.object(
                    muse, name,
                    return_value={
                        "argv": ["sandbox-exec", "-f", "POLICY",
                                 "EXE"],
                        "policy": dict(self.policy)})
            patcher.start()
            self.addCleanup(patcher.stop)

    def _fresh_root(self):
        root = os.path.realpath(
            tempfile.mkdtemp(prefix="msp-root-"))
        os.chmod(root, 0o700)
        self.addCleanup(shutil.rmtree, root, True)
        return root

    def _spawn_with(self, child):
        def _spawn(cmd, env, cwd):
            self.spawned.append((cmd, env, cwd))
            return child

        patcher = mock.patch.object(
            muse, "_msp_spawn_process", side_effect=_spawn)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(child.force_close)
        return child

    def _script(self, muse_home=None):
        home = muse_home or os.path.join(self.root, "data", "muse")
        return {
            "initialize": [_msp_test_ok(
                1, _msp_test_init_result(home))],
            "account/read": [_msp_test_ok(
                2, {"state": "loggedOut",
                    "credentialRequired": True,
                    "displayLabel": "must-be-dropped"})],
            "settings/read": [_msp_test_ok(
                3, {"schemaVersion": 1, "values": {
                    "run.workflow_trigger_mode": "auto",
                    "tools.artifact.enabled": True,
                    "tools.web_fetch.enabled": "yes",
                    "tools.web_search.mode": "client",
                    "TOKEN_X": "must-never-escape",
                    "apiKey": "must-never-escape",
                    "displayLabel": "me",
                    "nested": {"a": 1},
                    "tools.web_search.mode-wrong": 42}})],
            "permissionProfile/list": [_msp_test_ok(
                4, {"profiles": [
                    {"profileId": ":read-only", "label": "Reads",
                     "description": "d",
                     "reviewerAuthority": "human",
                     "available": True},
                    {"profileId": ":ask-me",
                     "available": False}]})],
            "mcpServer/list": [_msp_test_ok(
                5, {"servers": [
                    {"name": "s1", "status": "up",
                     "command": "must-never-escape",
                     "headers": {"A": "b"}},
                    "junk-entry"]})],
        }

    def _sent_methods(self, child):
        methods = []
        for line in child.raw_requests:
            obj = json.loads(line.decode("utf-8"))
            self.assertEqual(obj.get("jsonrpc"), "2.0")
            methods.append(obj.get("method"))
        return methods

    def test_collect_positive_and_cleanup_clean(self):
        child = _MspScriptedChild(script=self._script())
        self._spawn_with(child)
        handle = muse.spawn_msp_host(self.root)
        reads = muse.collect_msp_metadata(
            handle, timeout=30, app_version="test-1")
        cleanup = muse.msp_close(handle, reason="t")
        init = reads["initialize"]
        self.assertEqual(init["server_version"], "1.4.14")
        self.assertEqual(init["server_name"], "muse")
        self.assertEqual(init["schema_fingerprint"],
                         _MSP_PINNED_FP)
        self.assertEqual(init["muse_home"],
                         os.path.join(self.root, "data", "muse"))
        self.assertEqual(init["session_durability"], "ephemeral")
        self.assertTrue(init["experimental_api"])
        self.assertEqual(reads["account"],
                         {"state": "loggedOut",
                          "credential_required": True})
        lane, _reason = muse.msp_credential_lane(reads["account"])
        self.assertEqual(lane, "unknown")
        self.assertEqual(reads["settings"]["values"], {
            "run.workflow_trigger_mode": "auto",
            "tools.artifact.enabled": True,
            "tools.web_search.mode": "client"})
        self.assertEqual(reads["settings"]["dropped"], 6)
        self.assertEqual(len(reads["profiles"]["profiles"]), 2)
        self.assertEqual(
            reads["profiles"]["profiles"][1]["profile_id"],
            ":ask-me")
        self.assertEqual(reads["mcp"]["count"], 2)
        self.assertEqual(reads["mcp"]["servers"],
                         [{"index": 0, "name": "s1",
                           "status": "up"}])
        self.assertEqual(reads["mcp"]["fields_dropped"], 2)
        self.assertEqual(reads["mcp"]["malformed"], 1)
        self.assertEqual(cleanup["state"], "clean")
        self.assertEqual(cleanup["returncode"], 0)
        self.assertFalse(cleanup["signalled"])
        self.assertTrue(cleanup["joined_stdout"])
        self.assertTrue(cleanup["joined_stderr"])
        self.assertTrue(cleanup["stdout_eof"])
        self.assertTrue(cleanup["stderr_eof"])
        self.assertNotIn("host_broken", cleanup)
        self.assertEqual(
            self._sent_methods(child),
            ["initialize", "initialized", "account/read",
             "settings/read", "permissionProfile/list",
             "mcpServer/list"])
        self.assertEqual(child.requests[0]["params"], {
            "clientInfo": {"name": "openmainty",
                           "version": "test-1"},
            "capabilities": {"experimentalApi": True,
                             "requestedCapabilities": [],
                             "userInputDialogs": False}})
        for req in child.requests[1:]:
            self.assertEqual(req["params"], {})
        raw = b"\n".join(child.raw_requests)
        for token in (b"model", b"session", b"login", b"trace",
                      b"approval", b"TOKEN_X", b"must-never-escape"):
            self.assertNotIn(token, raw)
        self.assertEqual(len(self.spawned), 1)
        cmd, env, cwd = self.spawned[0]
        self.assertEqual(
            cmd[:5],
            ["sandbox-exec", "-f", "POLICY", "EXE", "serve"])
        self.assertIn("--no-session-log", cmd)
        self.assertIn("--disable-shell", cmd)
        self.assertIn("meta", cmd)
        self.assertEqual(set(env), {"HOME", "XDG_CONFIG_HOME",
                                    "XDG_DATA_HOME",
                                    "XDG_CACHE_HOME", "TMPDIR",
                                    "OPENMAINTY_HOME", "PATH"})
        self.assertEqual(env["HOME"],
                         os.path.join(self.root, "home"))
        self.assertEqual(cwd, self.root)
        with self.assertRaises(muse.MspError) as cm:
            muse.collect_msp_metadata(handle, timeout=5,
                                      app_version="t")
        self.assertEqual(cm.exception.code, "unknown-handle")

    def test_spawn_refuses_api_key_even_empty(self):
        started = []
        patcher = mock.patch.object(
            muse, "_msp_spawn_process",
            side_effect=lambda *a: started.append(a))
        patcher.start()
        self.addCleanup(patcher.stop)
        with mock.patch.dict(os.environ, {"META_API_KEY": ""}):
            with self.assertRaises(muse.MspError) as cm:
                muse.spawn_msp_host(self.root)
        self.assertEqual(cm.exception.code, "api-key-present")
        self.assertEqual(started, [])

    def test_spawn_refuses_unconfigured_pin_and_bad_input(self):
        with mock.patch.object(
                muse, "_msp_resolve_snapshot",
                side_effect=muse.MspError(
                    "no pin", code="binary-unavailable")):
            with self.assertRaises(muse.MspError) as cm:
                muse.spawn_msp_host(self.root)
        self.assertEqual(cm.exception.code, "binary-unavailable")

    def test_resolve_snapshot_converts_oserror(self):
        with mock.patch.object(
                muse, "resolve_pinned_muse",
                side_effect=OSError("slot empty")) as pinned_mock:
            with self.assertRaises(muse.MspError) as cm:
                self._real_resolve(None)
        self.assertEqual(cm.exception.code, "binary-unavailable")
        pinned_mock.assert_called_once_with(None)

    def test_spawn_rejects_bad_input(self):
        with open(os.path.join(self.root, "existing.txt"),
                   "wb") as fh:
            fh.write(b"reuse")
        with self.assertRaises(muse.MspError) as cm2:
            muse.spawn_msp_host(self.root)
        self.assertEqual(cm2.exception.code, "bad-input")
        for bad in (0, -1, float("inf"), float("nan"), "30",
                    True, None):
            with self.assertRaises(muse.MspError) as cm3:
                muse.spawn_msp_host(self._fresh_root(),
                                    timeout_s=bad)
            self.assertEqual(cm3.exception.code, "bad-input")

    def test_spawn_always_wraps_in_native_policy(self):
        if sys.platform != "darwin":
            self.skipTest("native wrapper is a macOS mechanism")
        with mock.patch.object(
                muse, "_msp_build_wrapper",
                side_effect=self._real_wrapper) as wrap_mock:
            child = _MspScriptedChild(script=self._script())
            self._spawn_with(child)
            handle = muse.spawn_msp_host(self.root)
            try:
                reads = muse.collect_msp_metadata(
                    handle, timeout=30, app_version="t")
            finally:
                cleanup = muse.msp_close(handle, reason="t")
        self.assertEqual(reads["account"]["state"], "loggedOut")
        self.assertEqual(cleanup["state"], "clean")
        wrap_mock.assert_called_once()
        cmd = self.spawned[0][0]
        self.assertTrue(cmd[0].endswith("sandbox-exec"))
        self.assertEqual(cmd[3], self.bin_path)
        self.assertEqual(cmd[4], "serve")
        self.assertIn("--no-session-log", cmd)
        policies = [name for name in os.listdir(self.root)
                    if name.endswith(".sb")]
        self.assertEqual(len(policies), 1)

    def test_init_strictness_no_later_query(self):
        base_variants = [
            ("bad-name", {"serverInfo": {"name": "x",
                                         "version": "1.4.14"}},
             "unknown-host"),
            ("bad-version", {"serverInfo": {"name": "muse",
                                            "version": "9.9"}},
             "unknown-host"),
            ("bad-fingerprint",
             {"schema": {"version": 1,
                         "fingerprint": "sha256:" + "00" * 32}},
             "unknown-schema"),
            ("bad-schema-version",
             {"schema": {"version": 2,
                         "fingerprint":
                             muse._MSP_SCHEMA_FINGERPRINT}},
             "unknown-schema"),
            ("durable",
             {"sessionDurability": "durable"}, "durability-not-ephemeral"),
            ("missing-durability",
             {"sessionDurability": None}, "durability-not-ephemeral"),
            ("experimental-false",
             {"experimentalApi": False}, "experimental-required"),
            ("foreign-home",
             {"museHome": "/elsewhere/data/muse"},
             "muse-home-mismatch"),
        ]
        for name, over, code in base_variants:
            with self.subTest(name=name):
                root = self._fresh_root()
                doc = _msp_test_init_result(
                    os.path.join(root, "data", "muse"))
                doc.update(over)
                if "sessionDurability" in over \
                        and over["sessionDurability"] is None:
                    del doc["sessionDurability"]
                child = _MspScriptedChild(
                    script={"initialize": [
                        _msp_test_ok(1, doc)]})
                self._spawn_with(child)
                handle = muse.spawn_msp_host(root)
                with self.assertRaises(muse.MspError) as cm:
                    muse.collect_msp_metadata(
                        handle, timeout=10, app_version="t")
                self.assertEqual(cm.exception.code, code, name)
                muse.msp_close(handle, reason="t")
                self.assertEqual(
                    self._sent_methods(child), ["initialize"],
                    name)

    def test_envelope_violations_break_host(self):
        cases = [
            ("missing-jsonrpc",
             _msp_test_line({"id": 1, "result": {}}),
             "malformed"),
            ("both-result-and-error",
             _msp_test_line({"jsonrpc": "2.0", "id": 1,
                             "result": {}, "error": {}}),
             "malformed"),
            ("neither-result-nor-error",
             _msp_test_line({"jsonrpc": "2.0", "id": 1}),
             "malformed"),
            ("bool-id",
             _msp_test_line({"jsonrpc": "2.0", "id": True,
                             "result": {}}),
             "malformed"),
            ("unknown-id",
             _msp_test_ok(99, {}),
             "unknown-id"),
            ("duplicate-id", "DUPLICATE", "duplicate-response"),
            ("server-request",
             _msp_test_line({"jsonrpc": "2.0", "id": 7,
                             "method": "approve",
                             "params": {}}),
             "server-request"),
            ("server-notification",
             _msp_test_line({"jsonrpc": "2.0",
                             "method": "tock"}),
             "server-notification"),
            ("account-changed-notification",
             _msp_test_line({"jsonrpc": "2.0",
                             "method": "account/changed"}),
             "account-changed"),
            ("framed-line-rejected",
             b"Content-Length: 10\r\n\r\n0123456789\n",
             "malformed"),
            ("duplicate-keys",
             b'{"jsonrpc":"2.0","id":1,"id":2,"result":{}}\n',
             "malformed"),
            ("nonfinite-constant",
             b'{"jsonrpc":"2.0","id":1,"result":NaN}\n',
             "malformed"),
            ("bad-utf8",
             b'{"jsonrpc":"2.0","id":1,"result":"\xff"}\n',
             "malformed"),
        ]
        for name, payload, code in cases:
            with self.subTest(name=name):
                root = self._fresh_root()
                home = os.path.join(root, "data", "muse")
                if payload == "DUPLICATE":
                    payload = (_msp_test_ok(
                        1, _msp_test_init_result(home))
                        + _msp_test_ok(1, {}))
                child = _MspScriptedChild(
                    script={"initialize": [payload]})
                self._spawn_with(child)
                handle = muse.spawn_msp_host(root)
                with self.assertRaises(muse.MspError) as cm:
                    muse.collect_msp_metadata(
                        handle, timeout=10, app_version="t")
                self.assertEqual(cm.exception.code, code, name)
                muse.msp_close(handle, reason="t")
                for line in child.raw_requests:
                    json.loads(line.decode("utf-8"))

    def test_binary_drift_refuses_same_inode_and_replaced(self):
        for name, tamper in (("same-inode-content", "inode"),
                             ("inode-replaced", "replace")):
            with self.subTest(name=name):
                root = self._fresh_root()
                child = _MspScriptedChild(
                    script=self._script(
                        os.path.join(root, "data", "muse")))
                self._spawn_with(child)
                handle = muse.spawn_msp_host(root)
                if tamper == "inode":
                    with open(self.bin_path, "r+b") as fh:
                        fh.write(b"XWNED-fixture-binary-bytes!")
                else:
                    other = self.bin_path + ".new"
                    with open(other, "wb") as fh:
                        fh.write(b"owned-fixture-binary-bytes")
                    os.replace(other, self.bin_path)
                try:
                    with self.assertRaises(muse.MspError) as cm:
                        muse.collect_msp_metadata(
                            handle, timeout=10, app_version="t")
                    self.assertEqual(
                        cm.exception.code, "binary-drift", name)
                finally:
                    muse.msp_close(handle, reason="t")

    def test_policy_drift_refuses(self):
        # Mirrors the binary drift matrix: a same-inode/same-size
        # byte change must fail on content-SHA, not just on stat
        # differences, and a replacement file must fail too. Each
        # subtest spawns against the CURRENT fixture bytes, so the
        # live content hash is always the authority — no validator
        # result is ever patched.
        cases = (
            ("same-inode-same-size", "inode"),
            ("replaced", "replace"),
        )
        for name, tamper in cases:
            with self.subTest(name=name):
                root = self._fresh_root()
                child = _MspScriptedChild(
                    script=self._script(
                        os.path.join(root, "data", "muse")))
                self._spawn_with(child)
                handle = muse.spawn_msp_host(root)
                if tamper == "inode":
                    # Same length (20B), different bytes, same
                    # inode: only the content SHA can catch this.
                    with open(self.policy_path, "r+b") as fh:
                        fh.write(b"FIXTURE-POLICY-BYTES")
                else:
                    other = self.policy_path + ".new"
                    with open(other, "wb") as fh:
                        fh.write(b"fixture-policy-bytes")
                    os.replace(other, self.policy_path)
                try:
                    with self.assertRaises(muse.MspError) as cm:
                        muse.collect_msp_metadata(
                            handle, timeout=10, app_version="t")
                    self.assertEqual(
                        cm.exception.code, "policy-changed", name)
                finally:
                    muse.msp_close(handle, reason="t")

    def test_host_exit_mid_sequence_refuses(self):
        child = _MspScriptedChild(script={})
        self._spawn_with(child)
        handle = muse.spawn_msp_host(self.root)
        child.alive = False
        with self.assertRaises(muse.MspError) as cm:
            muse.collect_msp_metadata(
                handle, timeout=10, app_version="t")
        self.assertEqual(cm.exception.code, "host-exited")
        muse.msp_close(handle, reason="t")

    def test_forged_and_closed_handles_refuse(self):
        with self.assertRaises(muse.MspError) as cm:
            muse.collect_msp_metadata("f" * 32, timeout=5,
                                      app_version="t")
        self.assertEqual(cm.exception.code, "unknown-handle")
        child = _MspScriptedChild(script=self._script())
        self._spawn_with(child)
        handle = muse.spawn_msp_host(self.root)
        muse.msp_close(handle, reason="t")
        with self.assertRaises(muse.MspError) as cm2:
            muse.collect_msp_metadata(handle, timeout=5,
                                      app_version="t")
        self.assertEqual(cm2.exception.code, "unknown-handle")
        with self.assertRaises(muse.MspError) as cm3:
            muse.msp_close(handle, reason="t")
        self.assertEqual(cm3.exception.code, "unknown-handle")

    def test_account_bind_and_notification_invalidation(self):
        rec = {"account_binding": None}
        muse._msp_bind_account(
            rec, {"state": "loggedOut",
                  "credential_required": True})
        self.assertEqual(rec["account_binding"],
                         ("loggedOut", True))
        muse._msp_bind_account(
            rec, {"state": "loggedOut",
                  "credential_required": True})
        with self.assertRaises(muse.MspError) as cm:
            muse._msp_bind_account(
                rec, {"state": "envKey",
                      "credential_required": True})
        self.assertEqual(cm.exception.code, "account-changed")
        root = self._fresh_root()
        home = os.path.join(root, "data", "muse")
        script = {"initialize": [_msp_test_ok(
            1, _msp_test_init_result(home))],
            "account/read": [_msp_test_line(
                {"jsonrpc": "2.0",
                 "method": "account/changed"})]}
        child = _MspScriptedChild(script=script)
        self._spawn_with(child)
        handle = muse.spawn_msp_host(root)
        with self.assertRaises(muse.MspError) as cm2:
            muse.collect_msp_metadata(
                handle, timeout=10, app_version="t")
        self.assertEqual(cm2.exception.code, "account-changed")
        self.assertEqual(
            self._sent_methods(child),
            ["initialize", "initialized", "account/read"])
        muse.msp_close(handle, reason="t")

    def test_cleanup_stubborn_child_only_owned_group(self):
        child = _MspScriptedChild(script=self._script(),
                                  wait_mode="none")
        self._spawn_with(child)
        handle = muse.spawn_msp_host(self.root)
        reads = muse.collect_msp_metadata(
            handle, timeout=30, app_version="t")
        self.assertEqual(reads["account"]["state"], "loggedOut")
        with mock.patch.object(
                muse, "_kill_owned_group",
                return_value=True) as kill_sink:
            cleanup = muse.msp_close(handle, reason="t")
        kill_sink.assert_called_once_with(_MspScriptedChild.FAKE_PID)
        self.assertEqual(cleanup["state"], "unknown")
        self.assertTrue(cleanup["signalled"])
        self.assertIsNone(cleanup["returncode"])
        self.assertTrue(cleanup["joined_stdout"])
        self.assertTrue(cleanup["joined_stderr"])
        with self.assertRaises(muse.MspError):
            muse.collect_msp_metadata(handle, timeout=5,
                                      app_version="t")

    def test_cleanup_broken_host_qualified(self):
        home = os.path.join(self.root, "data", "muse")
        child = _MspScriptedChild(
            script={"initialize": [_msp_test_ok(99, {})]})
        self._spawn_with(child)
        handle = muse.spawn_msp_host(self.root)
        with self.assertRaises(muse.MspError) as cm:
            muse.collect_msp_metadata(
                handle, timeout=10, app_version="t")
        self.assertEqual(cm.exception.code, "unknown-id")
        cleanup = muse.msp_close(handle, reason="t")
        self.assertEqual(cleanup["state"], "unknown")
        self.assertEqual(cleanup["host_broken"], "unknown-id")
        self.assertEqual(cleanup["returncode"], 0)
        # The reader stopped on the protocol break BEFORE physical
        # EOF (the write end stayed open until close): the thread
        # is joined but stdout never drained to EOF. That is the
        # honest unknown state — never clean.
        self.assertTrue(cleanup["joined_stdout"])
        self.assertTrue(cleanup["joined_stderr"])
        self.assertFalse(cleanup["stdout_eof"])
        self.assertTrue(cleanup["stderr_eof"])
        self.assertFalse(cleanup["signalled"])

    def test_write_requires_real_fd(self):
        proc = mock.Mock()
        proc.stdin = io.BytesIO(b"")
        with self.assertRaises(muse.MspError) as cm:
            muse._msp_write_all(
                proc, b"x", time.monotonic() + 5)
        self.assertEqual(cm.exception.code, "transport")

    def test_collect_timeout_overall_deadline(self):
        home = os.path.join(self.root, "data", "muse")
        child = _MspScriptedChild(
            script={"initialize": [_msp_test_ok(
                1, _msp_test_init_result(home))]},
            delay=5.0)
        self._spawn_with(child)
        handle = muse.spawn_msp_host(self.root)
        with self.assertRaises(muse.MspError) as cm:
            muse.collect_msp_metadata(
                handle, timeout=0.3, app_version="t")
        self.assertEqual(cm.exception.code, "timeout")
        cleanup = muse.msp_close(handle, reason="t")
        self.assertEqual(cleanup["state"], "unknown")
        self.assertEqual(cleanup.get("host_broken"), "timeout")
        self.assertTrue(child.join_responder(timeout=20))

    def _reader_rec(self, out_bytes, err_bytes=b""):
        proc = mock.Mock()
        proc.stdout = io.BytesIO(out_bytes)
        proc.stderr = io.BytesIO(err_bytes)
        return {"proc": proc, "inbox": [], "pending": {},
                "seen": set(), "broken": None,
                "break_lock": threading.Lock()}

    def test_stdout_flood_breaks_without_eof(self):
        # Newline-delimited blank frames: each line is within the
        # 1MiB line cap and skipped, so only the cumulative stdout
        # cap fires — no parser break precedes it.
        rec = self._reader_rec(
            b"\n" * (muse._MSP_STDOUT_MAX_BYTES + 1024))
        muse._msp_stdout_reader(rec)
        self.assertEqual(rec["broken"], "stdout-overflow")
        self.assertFalse(rec.get("stdout_eof"))
        self.assertGreater(rec.get("stdout_bytes", 0),
                           muse._MSP_STDOUT_MAX_BYTES)
        self.assertEqual(rec["inbox"], [])

    def test_line_cap_boundary_terminated_and_chunked(self):
        cap = muse._MSP_LINE_MAX_BYTES
        # Exactly at cap, terminated: accepted for dispatch.
        line, consumed = muse._split_msp_line(
            bytearray(b"y" * cap + b"\n"))
        self.assertEqual(line, b"y" * cap)
        self.assertEqual(consumed, cap + 1)
        # One byte over, terminated: refused despite the newline.
        with self.assertRaises(muse.MspError) as cm:
            muse._split_msp_line(
                bytearray(b"y" * (cap + 1) + b"\n"))
        self.assertEqual(cm.exception.code, "overflow")
        # One byte over, chunked (no terminator yet): refused too.
        with self.assertRaises(muse.MspError) as cm2:
            muse._split_msp_line(bytearray(b"y" * (cap + 1)))
        self.assertEqual(cm2.exception.code, "overflow")
        # Exactly at cap without terminator: needs more bytes.
        msg, used = muse._split_msp_line(bytearray(b"y" * cap))
        self.assertIsNone(msg)
        self.assertEqual(used, 0)
        # Partial message then clean EOF: truncation violation.
        proc = mock.Mock()
        proc.stdout = io.BytesIO(b'{"jsonrpc":"2.0"')
        proc.stderr = io.BytesIO(b"")
        rec = {"proc": proc, "inbox": [], "pending": {},
               "seen": set(), "broken": None,
               "break_lock": threading.Lock()}
        muse._msp_stdout_reader(rec)
        muse._msp_stderr_reader(rec)
        self.assertTrue(rec.get("stdout_eof"))
        self.assertEqual(rec["broken"], "truncated-line")
        self.assertTrue(rec.get("stderr_eof"))
        # Terminated truly-empty lines are skipped, not
        # dispatched; nothing accepted, nothing broken. A
        # whitespace-only line is NOT valid JSON and breaks as
        # malformed — the skip applies to b"" only.
        proc2 = mock.Mock()
        proc2.stdout = io.BytesIO(b"\n\n")
        proc2.stderr = io.BytesIO(b"")
        rec2 = {"proc": proc2, "inbox": [], "pending": {},
                "seen": set(), "broken": None,
                "break_lock": threading.Lock()}
        muse._msp_stdout_reader(rec2)
        self.assertTrue(rec2.get("stdout_eof"))
        self.assertIsNone(rec2.get("broken"))
        self.assertEqual(rec2["inbox"], [])
        proc3 = mock.Mock()
        proc3.stdout = io.BytesIO(b"   \n")
        proc3.stderr = io.BytesIO(b"")
        rec3 = {"proc": proc3, "inbox": [], "pending": {},
                "seen": set(), "broken": None,
                "break_lock": threading.Lock()}
        muse._msp_stdout_reader(rec3)
        self.assertEqual(rec3["broken"], "malformed")

    def test_valid_exchange_then_partial_tail_cleanup_unknown(self):
        # A full valid exchange succeeds first; then an unterminated
        # tail arrives and stdin EOF (close) ends the responder. The
        # reader reaches real EOF with residue, so cleanup must
        # report unknown with truncated-line — never clean.
        home = os.path.join(self.root, "data", "muse")
        child = _MspScriptedChild(
            script={"initialize": [_msp_test_ok(
                1, _msp_test_init_result(home))]})
        self._spawn_with(child)
        handle = muse.spawn_msp_host(self.root)
        result = muse._msp_send_request(
            handle, "initialize",
            {"clientInfo": {"name": "t", "version": "t"},
             "capabilities": {"experimentalApi": True,
                              "requestedCapabilities": [],
                              "userInputDialogs": False}},
            10)
        self.assertIsInstance(result, dict)
        self.assertTrue(result)
        os.write(child._out_w, b'{"jsonrpc":"2.0","id":99,')
        cleanup = muse.msp_close(handle, reason="t")
        self.assertEqual(cleanup["state"], "unknown")
        self.assertEqual(cleanup.get("host_broken"), "truncated-line")
        self.assertTrue(cleanup["stdout_eof"])

    def test_stderr_flood_breaks_and_stops(self):
        rec = self._reader_rec(
            b"", b"y" * (muse._MSP_STDERR_MAX_BYTES + 1024))
        muse._msp_stdout_reader(rec)
        muse._msp_stderr_reader(rec)
        self.assertTrue(rec.get("stdout_eof"))
        self.assertEqual(rec["broken"], "stderr-overflow")
        self.assertFalse(rec.get("stderr_eof"))
        self.assertGreater(rec.get("stderr_bytes", 0),
                           muse._MSP_STDERR_MAX_BYTES)

    def _break_rec(self):
        return {"proc": mock.Mock(), "inbox": [], "pending": {},
                "seen": set(), "broken": None,
                "break_lock": threading.Lock()}

    def test_first_break_wins_across_dispatch_and_account(self):
        rec = self._break_rec()
        muse._msp_bind_account(
            rec, {"state": "loggedOut",
                  "credential_required": True})
        self.assertIsNone(rec.get("broken"))
        # A protocol break (unknown response id) lands first.
        muse._dispatch_msp_inbox(
            rec, b'{"jsonrpc":"2.0","id":7,"result":{}}')
        self.assertEqual(rec["broken"], "unknown-id")
        # A later timeout attempt must not overwrite the cause...
        self.assertEqual(muse._msp_break(rec, "timeout"),
                         "unknown-id")
        self.assertEqual(rec["broken"], "unknown-id")
        # ...nor may a later account drift, though it still raises.
        with self.assertRaises(muse.MspError) as cm:
            muse._msp_bind_account(
                rec, {"state": "envKey",
                      "credential_required": True})
        self.assertEqual(cm.exception.code, "account-changed")
        self.assertEqual(rec["broken"], "unknown-id")
        # The reverse order sticks too: account drift first wins
        # over a later protocol break.
        rec2 = self._break_rec()
        muse._msp_bind_account(
            rec2, {"state": "loggedOut",
                   "credential_required": True})
        with self.assertRaises(muse.MspError):
            muse._msp_bind_account(
                rec2, {"state": "envKey",
                       "credential_required": True})
        self.assertEqual(rec2["broken"], "account-changed")
        muse._dispatch_msp_inbox(
            rec2, b'{"jsonrpc":"2.0","id":7,"result":{}}')
        self.assertEqual(rec2["broken"], "account-changed")

    def test_stderr_break_survives_request_timeout(self):
        rec = self._reader_rec(
            b"", b"y" * (muse._MSP_STDERR_MAX_BYTES + 16))
        muse._msp_stderr_reader(rec)
        self.assertEqual(rec["broken"], "stderr-overflow")
        self.assertEqual(muse._msp_break(rec, "timeout"),
                         "stderr-overflow")
        self.assertEqual(rec["broken"], "stderr-overflow")

    def test_concurrent_break_single_winner(self):
        rec = self._break_rec()
        codes = ["timeout", "unknown-id", "stderr-overflow",
                 "account-changed", "truncated-line"]
        observed = []
        guard = threading.Lock()
        barrier = threading.Barrier(len(codes))

        def _race(code):
            # All contenders start together; the break call itself
            # runs OUTSIDE the observation guard so the threads
            # genuinely race on the record lock — only the append
            # is serialized.
            barrier.wait(timeout=15)
            result = muse._msp_break(rec, code)
            with guard:
                observed.append(result)

        threads = [threading.Thread(target=_race, args=(code,))
                   for code in codes]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        for thread in threads:
            self.assertFalse(thread.is_alive())
        # Exactly one cause won, and every racer observed it:
        # earliest-break provenance is synchronized, not last-write.
        self.assertIn(rec["broken"], codes)
        self.assertEqual(len(observed), len(codes))
        for value in observed:
            self.assertEqual(value, rec["broken"])

    def test_notify_deadline_fixed_before_live_check(self):
        child = _MspScriptedChild(script=self._script())
        self._spawn_with(child)
        handle = muse.spawn_msp_host(self.root)
        now = [1000.0]
        captured = {}
        real_write = muse._msp_write_all
        real_check = muse._msp_check_live

        def _spy_write(proc, data, deadline):
            captured["deadline"] = deadline
            return real_write(proc, data, deadline)

        def _slow_check(rec):
            # The real identity revalidation still runs, but it
            # consumes (fake) time first: a deadline fixed only
            # after the check would inherit the 5s drift.
            now[0] += 5.0
            return real_check(rec)

        with mock.patch.object(muse.time, "monotonic",
                               side_effect=lambda: now[0]), \
                mock.patch.object(muse, "_msp_write_all",
                                  side_effect=_spy_write), \
                mock.patch.object(muse, "_msp_check_live",
                                  side_effect=_slow_check):
            muse._msp_send_notify(handle, "initialized", 10)
        # Fixed at entry (1000 + 10), not after the check (1015).
        self.assertEqual(captured.get("deadline"), 1010.0)
        muse.msp_close(handle, reason="t")

    def test_request_refuses_response_after_entry_deadline(self):
        child = _MspScriptedChild(
            script={"settings/read": [_msp_test_ok(
                1, {"late": True})]})
        self._spawn_with(child)
        handle = muse.spawn_msp_host(self.root)
        rec = muse._host_record(handle)
        now = [2000.0]
        real_monotonic = time.monotonic
        real_write = muse._msp_write_all
        outcome = {}

        def _spy_write(proc, data, deadline):
            # ALWAYS perform the real write first: the request is
            # genuinely on the wire through the real pipe.
            result = real_write(proc, data, deadline)
            # Wait until the REAL reader thread queued the valid
            # matching reply (bounded, real clock)...
            start = real_monotonic()
            while not any(obj.get("id") == 1
                          for obj in rec["inbox"]):
                if real_monotonic() - start > 15:
                    raise AssertionError(
                        "reply never reached the reader")
                time.sleep(0.005)
            # ...THEN expire the entry deadline before returning to
            # the request loop. The queued answer is now late and
            # must be refused. An implementation that reset the
            # deadline after the write would incorrectly accept it.
            now[0] += 30.0
            return result

        def _caller():
            try:
                muse._msp_send_request(
                    handle, "settings/read", {}, 10)
            except muse.MspError as exc:
                outcome["error"] = exc
            else:
                outcome["value"] = True

        with mock.patch.object(muse.time, "monotonic",
                               side_effect=lambda: now[0]), \
                mock.patch.object(muse, "_msp_write_all",
                                  side_effect=_spy_write):
            worker = threading.Thread(target=_caller, daemon=True)
            worker.start()
            worker.join(timeout=20)
        self.assertFalse(worker.is_alive())
        self.assertIn("error", outcome)
        self.assertEqual(outcome["error"].code, "timeout")
        self.assertEqual(rec.get("broken"), "timeout")
        self.assertEqual(rec.get("pending"), {})
        # The late answer really was queued by the reader — and
        # refused, not consumed as a live result.
        self.assertTrue(any(obj.get("id") == 1
                            for obj in rec["inbox"]))
        muse.msp_close(handle, reason="t")

    def test_collect_late_final_response_is_not_accepted(self):
        home = os.path.join(self.root, "data", "muse")
        full = self._script(home)
        script = {key: value for key, value in full.items()
                  if key != "mcpServer/list"}
        child = _MspScriptedChild(script=script)
        self._spawn_with(child)
        handle = muse.spawn_msp_host(self.root)
        rec = muse._host_record(handle)
        now = [3000.0]
        real_monotonic = time.monotonic
        real_write = muse._msp_write_all
        late = (b'{"jsonrpc":"2.0","id":5,'
                b'"result":{"servers":[]}}\n')

        def _spy_write(proc, data, deadline):
            # EVERY write is genuinely performed first — no
            # request is faked. Only the final method gets the
            # late-answer arrangement below.
            result = real_write(proc, data, deadline)
            try:
                method = json.loads(
                    data[:-1].decode("utf-8")).get("method")
            except (ValueError, UnicodeDecodeError):
                method = None
            if method != "mcpServer/list":
                return result
            # The final request is on the wire. Deliver the valid
            # final answer through the REAL pipe/reader route and
            # wait until it is actually queued...
            os.write(child._out_w, late)
            start = real_monotonic()
            while not any(obj.get("id") == 5
                          for obj in rec["inbox"]):
                if real_monotonic() - start > 15:
                    raise AssertionError(
                        "late reply never reached the reader")
                time.sleep(0.005)
            # ...THEN expire the overall deadline before the loop
            # decides. Old code returning the queued reply here
            # would incorrectly accept it as live.
            now[0] += 30.0
            return result

        with mock.patch.object(muse.time, "monotonic",
                               side_effect=lambda: now[0]), \
                mock.patch.object(muse, "_msp_write_all",
                                  side_effect=_spy_write):
            with self.assertRaises(muse.MspError) as cm:
                muse.collect_msp_metadata(
                    handle, timeout=10, app_version="t")
        self.assertEqual(cm.exception.code, "timeout")
        # The late answer really was queued by the reader — and
        # refused at the request level, so the break is recorded
        # as timeout, not silently absorbed.
        self.assertTrue(any(obj.get("id") == 5
                            for obj in rec["inbox"]))
        self.assertEqual(rec.get("broken"), "timeout")
        cleanup = muse.msp_close(handle, reason="t")
        self.assertEqual(cleanup.get("host_broken"), "timeout")


class MspDiagnosticCase(unittest.TestCase):
    """diagnose_msp_metadata: fresh child root, clean-close gating.

    Same IO-boundary seams as the factory case (fixture binary over
    a real temp file, fixture wrapper identity, fake child on real
    os.pipes). Nothing is persisted: every test asserts no
    preflight/input records appear under the caller home, the
    caller home itself is never used as the child HOME, and
    admits_worker stays False on every path.
    """

    def setUp(self):
        base = tempfile.mkdtemp(prefix="msp-diag-")
        self.work = os.path.realpath(base)
        os.chmod(self.work, 0o700)
        self.addCleanup(shutil.rmtree, self.work, True)
        home = tempfile.mkdtemp(prefix="msp-home-")
        self.home = os.path.realpath(home)
        os.chmod(self.home, 0o700)
        self.addCleanup(shutil.rmtree, self.home, True)
        with open(os.path.join(self.home, "existing.json"),
                   "wb") as fh:
            fh.write(b'{"existing": true}')
        prev = os.environ.pop("META_API_KEY", None)

        def _restore():
            if prev is None:
                os.environ.pop("META_API_KEY", None)
            else:
                os.environ["META_API_KEY"] = prev

        self.addCleanup(_restore)
        self.bin_path = os.path.join(self.work, "muse-bin")
        with open(self.bin_path, "wb") as fh:
            fh.write(b"owned-fixture-binary-bytes")
        os.chmod(self.bin_path, 0o700)
        bst = os.stat(self.bin_path)
        self.snap = {"path": self.bin_path,
                     "real_path": self.bin_path,
                     "sha256": hashlib.sha256(
                         b"owned-fixture-binary-bytes").hexdigest(),
                     "dev": bst.st_dev, "ino": bst.st_ino,
                     "size": bst.st_size}
        self.policy_path = os.path.join(self.work, "POLICY.sb")
        with open(self.policy_path, "wb") as fh:
            fh.write(b"fixture-policy-bytes")
        pst = os.stat(self.policy_path)
        self.policy = {"path": self.policy_path,
                       "sha256": hashlib.sha256(
                           b"fixture-policy-bytes").hexdigest(),
                       "size": pst.st_size, "dev": pst.st_dev,
                       "ino": pst.st_ino}
        for name, value in (
                ("_msp_resolve_snapshot", dict(self.snap)),
                ("_msp_build_wrapper",
                 {"argv": ["sandbox-exec", "-f", "POLICY", "EXE"],
                  "policy": dict(self.policy)})):
            patcher = mock.patch.object(muse, name,
                                        return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.children = []
        self.spawned = []

    def _script_for(self, muse_home):
        return {
            "initialize": [_msp_test_ok(
                1, _msp_test_init_result(muse_home))],
            "account/read": [_msp_test_ok(
                2, {"state": "loggedOut",
                    "credentialRequired": True})],
            "settings/read": [_msp_test_ok(
                3, {"schemaVersion": 1, "values": {
                    "run.workflow_trigger_mode": "auto",
                    "tools.artifact.enabled": True,
                    "tools.web_fetch.enabled": False,
                    "tools.web_search.mode": "client"}})],
            "permissionProfile/list": [_msp_test_ok(
                4, {"profiles": []})],
            "mcpServer/list": [_msp_test_ok(5, {"servers": []})],
        }

    def _spawn_capturing(self, make_child):
        def _spawn(cmd, env, cwd):
            child = make_child(cwd)
            self.children.append(child)
            self.spawned.append((cmd, env, cwd))
            self.addCleanup(child.force_close)
            return child

        patcher = mock.patch.object(
            muse, "_msp_spawn_process", side_effect=_spawn)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _spawn_valid(self, **child_kwargs):
        def _make(cwd):
            return _MspScriptedChild(
                script=self._script_for(
                    os.path.join(cwd, "data", "muse")),
                **child_kwargs)

        self._spawn_capturing(_make)

    def test_diagnose_positive_ok(self):
        self._spawn_valid()
        out = pf.diagnose_msp_metadata(self.home, timeout_s=30)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["code"], "metadata-observed")
        self.assertEqual(set(out["reads"]),
                         {"initialize", "account", "settings",
                          "profiles", "mcp"})
        self.assertEqual(out["credential_lane"], "unknown")
        self.assertTrue(out["lane_reason"])
        self.assertEqual(out["cleanup"]["state"], "clean")
        self.assertFalse(out["admits_worker"])
        self.assertEqual(out["host"]["sha256"],
                         self.snap["sha256"])
        self.assertEqual(out["host"]["pid"],
                         _MspScriptedChild.FAKE_PID)
        self.assertEqual(len(out["host"]["nonce"]), 16)
        self.assertEqual(len(self.spawned), 1)
        _cmd, env, child_root = self.spawned[0]
        self.assertTrue(child_root.startswith(self.home + os.sep))
        self.assertIn("msp-child-", child_root)
        self.assertEqual(env["HOME"],
                         os.path.join(child_root, "home"))
        self.assertEqual(
            env["OPENMAINTY_HOME"], child_root)
        self.assertNotIn("META_API_KEY", env)
        self.assertFalse(os.path.exists(
            os.path.join(self.home, "preflight")))
        self.assertTrue(os.path.exists(
            os.path.join(self.home, "existing.json")))

    def test_diagnose_refuses_api_key(self):
        started = []
        patcher = mock.patch.object(
            muse, "_msp_spawn_process",
            side_effect=lambda *a: started.append(a))
        patcher.start()
        self.addCleanup(patcher.stop)
        with mock.patch.dict(os.environ, {"META_API_KEY": ""}):
            out = pf.diagnose_msp_metadata(self.home)
        self.assertEqual(out["status"], "refused")
        self.assertEqual(out["code"], "api-key-present")
        self.assertEqual(started, [])
        self.assertFalse(out["admits_worker"])

    def test_diagnose_bad_home_and_timeout(self):
        with self.assertRaises(pf.PreflightError):
            pf.diagnose_msp_metadata(
                os.path.join(self.home, "missing"))
        leaf = os.path.join(self.home, "file-home")
        with open(leaf, "wb") as fh:
            fh.write(b"x")
        with self.assertRaises(pf.PreflightError):
            pf.diagnose_msp_metadata(leaf)
        with self.assertRaises(pf.PreflightError):
            pf.diagnose_msp_metadata(self.home,
                                     timeout_s=float("inf"))

    def test_diagnose_cleanup_incomplete_qualified(self):
        self._spawn_valid(wait_mode="none")
        with mock.patch.object(
                muse, "_kill_owned_group",
                return_value=True):
            out = pf.diagnose_msp_metadata(self.home,
                                           timeout_s=30)
        self.assertEqual(out["status"], "unknown")
        self.assertEqual(out["code"], "cleanup-incomplete")
        self.assertEqual(set(out["reads"]),
                         {"initialize", "account", "settings",
                          "profiles", "mcp"})
        self.assertEqual(out["credential_lane"], "unknown")
        self.assertTrue(out["cleanup"]["signalled"])
        self.assertFalse(out["admits_worker"])

    def test_diagnose_durable_refused(self):
        def _make(cwd):
            home = os.path.join(cwd, "data", "muse")
            return _MspScriptedChild(script={"initialize": [
                _msp_test_ok(1, _msp_test_init_result(
                    home, sessionDurability="durable"))]})

        self._spawn_capturing(_make)
        out = pf.diagnose_msp_metadata(self.home, timeout_s=30)
        self.assertEqual(out["status"], "unknown")
        self.assertEqual(out["code"], "durability-not-ephemeral")
        self.assertEqual(out["reads"], {})
        self.assertFalse(out["admits_worker"])

    def test_diagnose_containment_refused(self):
        started = []
        with mock.patch.object(
                muse, "_msp_build_wrapper",
                side_effect=muse.MspError("no wrapper",
                                          code="containment")):
            with mock.patch.object(
                    muse, "_msp_spawn_process",
                    side_effect=lambda *a: started.append(a)):
                out = pf.diagnose_msp_metadata(self.home,
                                               timeout_s=30)
        self.assertEqual(out["status"], "refused")
        self.assertEqual(out["code"], "containment")
        self.assertEqual(started, [])

    def test_diagnose_unexpected_error_still_closes(self):
        self._spawn_valid()
        with mock.patch.object(
                muse, "_msp_send_request",
                side_effect=ValueError("boom")):
            out = pf.diagnose_msp_metadata(self.home,
                                           timeout_s=30)
        self.assertEqual(out["status"], "unknown")
        self.assertEqual(out["code"], "sequence-failed")
        self.assertNotIn("boom", out["reason"])
        self.assertEqual(out["reads"], {})
        self.assertFalse(out["admits_worker"])
        self.assertEqual(len(self.children), 1)
        child = self.children[0]
        self.assertTrue(child.stdout.closed)
        self.assertTrue(child.stderr.closed)
        self.assertTrue(child.wait_calls)
        self.assertEqual(muse._MSP_HOSTS, {})

    def test_diagnose_nonzero_exit_qualified(self):
        self._spawn_valid(exit_code=3)
        out = pf.diagnose_msp_metadata(self.home, timeout_s=30)
        self.assertEqual(out["status"], "unknown")
        self.assertEqual(out["code"], "cleanup-incomplete")
        self.assertEqual(out["cleanup"]["returncode"], 3)
        self.assertEqual(out["cleanup"]["state"], "unknown")
        self.assertEqual(set(out["reads"]),
                         {"initialize", "account", "settings",
                          "profiles", "mcp"})
        self.assertFalse(out["admits_worker"])


_MSP_WORKER_SID_A = "0193e0f0-7a2b-7c3d-9e4f-5a6b7c8d9e0f"
_MSP_WORKER_SID_B = "0193e0f1-8c4d-7d5e-a1b2-c3d4e5f60718"


def _msp_worker_session_doc(sid, root, **over):
    doc = {"sessionId": sid, "providerId": "meta",
           "modelId": "muse-spark-1.3",
           "workspaceRoot": root, "status": "idle",
           "activeTurnId": None, "turnCount": 0,
           "createdAt": "2026-01-01T00:00:00Z",
           "updatedAt": "2026-01-01T00:00:00Z",
           "forkedFrom": None, "path": ""}
    doc.update(over)
    return doc


def _msp_worker_start_result(sid, root, **over):
    return {"session": _msp_worker_session_doc(sid, root, **over),
            "viewCursor": "vc-start-1"}


def _msp_worker_read_result(sid, root, **over):
    # Schema-valid HistoryMode "inline" with an empty item array
    # and a null snapshot (never mode "m": HistoryMode is the
    # closed served-what enum anchoredSnapshot/inline/snapshot/
    # none, open only toward newer unknown values).
    return {"history": {"items": [], "mode": "inline",
                        "snapshot": None},
            "pendingRequests": [],
            "session": _msp_worker_session_doc(sid, root, **over),
            "viewCursor": "vc-read-1"}


class MspWorkerSessionCase(unittest.TestCase):
    """Owned worker transport/session lifecycle (Phase 1, offline).

    Fake child on real os.pipes with strict autospec seams; every
    parser, registry check, workspace proof, lifecycle ledger, and
    capability refusal runs for real. Session ids are realistic
    UUIDv7 fixtures (version/variant checked), never bare strings.
    No model turn is ever written: the turn path must refuse with
    no wire write.
    """

    def setUp(self):
        base = tempfile.mkdtemp(prefix="msp-worker-")
        self.work = os.path.realpath(base)
        os.chmod(self.work, 0o700)
        self.addCleanup(shutil.rmtree, self.work, True)
        root = tempfile.mkdtemp(prefix="msp-wroot-")
        self.root = os.path.realpath(root)
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        prev = os.environ.pop("META_API_KEY", None)

        def _restore():
            if prev is None:
                os.environ.pop("META_API_KEY", None)
            else:
                os.environ["META_API_KEY"] = prev

        self.addCleanup(_restore)
        self.bin_path = os.path.join(self.work, "muse-bin")
        with open(self.bin_path, "wb") as fh:
            fh.write(b"owned-fixture-binary-bytes")
        os.chmod(self.bin_path, 0o700)
        bst = os.stat(self.bin_path)
        self.snap = {"path": self.bin_path,
                     "real_path": self.bin_path,
                     "sha256": hashlib.sha256(
                         b"owned-fixture-binary-bytes").hexdigest(),
                     "dev": bst.st_dev, "ino": bst.st_ino,
                     "size": bst.st_size}
        self.policy_path = os.path.join(self.work, "POLICY.sb")
        with open(self.policy_path, "wb") as fh:
            fh.write(b"fixture-policy-bytes")
        pst = os.stat(self.policy_path)
        self.policy = {"path": self.policy_path,
                       "sha256": hashlib.sha256(
                           b"fixture-policy-bytes").hexdigest(),
                       "size": pst.st_size, "dev": pst.st_dev,
                       "ino": pst.st_ino}
        for name, value in (
                ("_msp_resolve_snapshot", dict(self.snap)),
                ("_msp_build_wrapper",
                 {"argv": ["sandbox-exec", "-f", "POLICY", "EXE"],
                  "policy": dict(self.policy)})):
            patcher = mock.patch.object(muse, name,
                                        return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.spawned = []
        self.children = []
        # Worker seam patchers active for the CURRENT bootstrap
        # only. A second bootstrap in the same test must stop the
        # previous ones first: autospec-ing an already-mocked
        # attribute raises InvalidSpecError, and stale return
        # values would mask the fresh root contract. Stopped
        # exactly once via the single teardown cleanup below —
        # never double-stop.
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
            muse, "_msp_spawn_process", autospec=True,
            side_effect=_spawn)
        patcher.start()
        self._worker_patchers.append(patcher)

    def _methods(self, child):
        out = []
        for line in child.raw_requests:
            out.append(json.loads(line.decode("utf-8")))
        return out

    def _workspace(self, name="ws"):
        path = os.path.join(self.root, name)
        os.mkdir(path, 0o700)
        return path

    def _bootstrap_script(self, muse_home):
        return {
            "initialize": [_msp_test_ok(
                1, _msp_test_init_result(muse_home))],
            "account/read": [_msp_test_ok(
                2, {"state": "loggedOut",
                    "credentialRequired": True})],
        }

    def _bootstrap(self, child_script=None, **child_kwargs):
        # Every worker gets a UNIQUE fresh root: a spawn creates
        # home/config/data/tmp/policy inside it, and the production
        # fresh-empty-root contract would refuse a reused root
        # before any targeted assertion. Never loosen that check
        # or delete an active root; dead roots are removed by
        # cleanup only after their handle closed.
        # A repeated bootstrap in the same test first stops the
        # previous seam patchers (autospec-ing an already-mocked
        # attribute is an InvalidSpecError, never a product
        # defect), then installs fresh ones against the real seam.
        self._stop_worker_patches()
        root = os.path.realpath(
            tempfile.mkdtemp(prefix="msp-wroot-"))
        os.chmod(root, 0o700)
        self.addCleanup(shutil.rmtree, root, True)
        self.root = root
        home = os.path.join(root, "data", "muse")
        child = _MspScriptedChild(
            script=child_script or self._bootstrap_script(home),
            **child_kwargs)
        # The fake child exists now: its cleanup is registered
        # BEFORE any patch or spawn operation below can fail, so
        # a partial fixture can never leak its responder thread.
        self.addCleanup(child.force_close)
        for name, value in (
                ("_msp_resolve_snapshot", dict(self.snap)),
                ("_msp_build_wrapper",
                 {"argv": ["sandbox-exec", "-f", "POLICY",
                           "EXE"],
                  "policy": dict(self.policy)})):
            patcher = mock.patch.object(muse, name,
                                        return_value=value)
            patcher.start()
            self._worker_patchers.append(patcher)
        self._spawn_with(child)
        handle = muse.spawn_msp_worker(root)
        out = muse.msp_worker_bootstrap(
            handle, timeout=30, app_version="t")
        self.assertEqual(out["credential_lane"], "unknown")
        return handle, child

    def _poll_ledger(self, handle, count, timeout=10):
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            rec = muse._MSP_WORKERS.get(handle)
            if rec is not None and len(rec.get("lifecycle",
                                               [])) >= count:
                return list(rec["lifecycle"])
            time.sleep(0.01)
        self.fail("lifecycle ledger never reached %d" % count)

    def test_uuid_v7_shape_and_time(self):
        before_ms = time.time_ns() // 1000000
        first = muse._msp_new_command_id()
        second = muse._msp_new_command_id()
        after_ms = time.time_ns() // 1000000
        self.assertNotEqual(first, second)
        for value in (first, second):
            muse._msp_check_uuid(value, "command id", version="7")
            raw = value.replace("-", "")
            stamp_ms = int(raw[0:12], 16)
            self.assertGreaterEqual(stamp_ms, before_ms - 1000)
            self.assertLessEqual(stamp_ms, after_ms + 1000)
            self.assertEqual(raw[12], "7")
            self.assertIn(raw[16], "89abAB")
        with self.assertRaises(muse.MspError):
            muse._msp_check_uuid("not-a-uuid", "command id",
                                 version="7")
        with self.assertRaises(muse.MspError):
            muse._msp_check_uuid(_MSP_WORKER_SID_A.replace(
                "-7c3d-", "-6c3d-"), "command id", version="7")

    def test_bootstrap_start_read_hooks_same_pipe(self):
        handle, child = self._bootstrap()
        ws = self._workspace()
        start_script = _msp_worker_start_result(
            _MSP_WORKER_SID_A, ws)
        child._script["session/start"] = [_msp_test_ok(3,
                                                       start_script)]
        child._script["session/read"] = [_msp_test_ok(
            4, _msp_worker_read_result(_MSP_WORKER_SID_A, ws))]
        # Legitimate empty hook set: no partial fake row is
        # admitted as a complete schema-valid HookCatalogEntry.
        child._script["hook/list"] = [_msp_test_ok(
            5, {"hooks": []})]
        bound = muse.msp_worker_session_start(
            handle, workspace_root=ws, timeout=30)
        self.assertEqual(bound["id"], _MSP_WORKER_SID_A)
        self.assertEqual(bound["workspace_root"], ws)
        # The responder also records the id-less initialized
        # notification, so select the start request by method,
        # never by a fixed index.
        starts = [obj for obj in self._methods(child)
                  if obj.get("method") == "session/start"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(set(starts[0]["params"]),
                         {"workspaceRoot", "commandId",
                          "providerId", "modelId"})
        self.assertEqual(starts[0]["params"]["providerId"],
                         "meta")
        self.assertEqual(starts[0]["params"]["modelId"],
                         "muse-spark-1.3")
        self.assertEqual(starts[0]["params"]["workspaceRoot"],
                         ws)
        self.assertNotIn("permissionProfile",
                         starts[0]["params"])
        self.assertNotIn("config", starts[0]["params"])
        self.assertNotIn("sessionId", starts[0]["params"])
        self.assertNotIn("approvalMode", starts[0]["params"])
        seen = muse.msp_worker_session_read(handle, timeout=30)
        self.assertEqual(seen["session"]["id"],
                         _MSP_WORKER_SID_A)
        self.assertEqual(seen["history_items"], 0)
        hooks = muse.msp_worker_hooks(handle, timeout=30)
        self.assertEqual(hooks, {"session_id": _MSP_WORKER_SID_A,
                                 "count": 0})
        cap = muse.msp_worker_capability(handle)
        self.assertFalse(cap["admitted"])
        self.assertTrue(cap["session_bound"])
        self.assertEqual(cap["session_status"], "idle")
        self.assertTrue(any("winning-credential" in item
                            for item in cap["missing"]))
        methods = [obj["method"] for obj in
                   self._methods(child)]
        self.assertEqual(
            methods,
            ["initialize", "initialized", "account/read",
             "session/start", "session/read", "hook/list"])
        self.assertEqual(methods.count("initialized"), 1)
        cleanup = muse.msp_worker_close(handle, reason="t")
        self.assertEqual(cleanup["state"], "clean")
        self.assertNotIn(handle, muse._MSP_WORKERS)

    def test_session_start_requires_bootstrap(self):
        child = _MspScriptedChild(script={})
        self.addCleanup(child.force_close)
        self._spawn_with(child)
        handle = muse.spawn_msp_worker(self.root)
        try:
            with self.assertRaises(muse.MspError) as cm:
                muse.msp_worker_session_start(
                    handle, workspace_root=self._workspace(),
                    timeout=10)
            self.assertEqual(cm.exception.code,
                             "not-bootstrapped")
            self.assertEqual(child.raw_requests, [])
        finally:
            muse.msp_worker_close(handle, reason="t")

    def test_session_start_rejects_foreign_and_dirty_workspace(self):
        handle, _child = self._bootstrap()
        try:
            outside = tempfile.mkdtemp(prefix="msp-foreign-")
            outside = os.path.realpath(outside)
            self.addCleanup(shutil.rmtree, outside, True)
            with self.assertRaises(muse.MspError) as cm:
                muse.msp_worker_session_start(
                    handle, workspace_root=outside, timeout=10)
            self.assertEqual(cm.exception.code,
                             "workspace-foreign")
            dirty = self._workspace("dirty")
            with open(os.path.join(dirty, "x"), "wb") as fh:
                fh.write(b"x")
            with self.assertRaises(muse.MspError) as cm2:
                muse.msp_worker_session_start(
                    handle, workspace_root=dirty, timeout=10)
            self.assertEqual(cm2.exception.code,
                             "workspace-not-empty")
            rec = muse._MSP_WORKERS[handle]
            self.assertIsNone(rec.get("worker_session"))
        finally:
            muse.msp_worker_close(handle, reason="t")

    def test_session_start_rejects_non_idle_and_drift(self):
        cases = (
            ("running-turn",
             {"activeTurnId": _MSP_WORKER_SID_B},
             "session-not-idle"),
            ("running-status",
             {"status": "running"}, "session-not-idle"),
            ("used-turns", {"turnCount": 1},
             "session-not-idle"),
            ("nonempty-path", {"path": "/tmp/x"},
             "session-not-idle"),
            ("provider-drift", {"providerId": "other"},
             "provider-mismatch"),
            ("model-drift", {"modelId": "other-model"},
             "model-mismatch"),
        )
        for name, over, code in cases:
            with self.subTest(name=name):
                handle, child = self._bootstrap()
                try:
                    ws = self._workspace("ws-" + name)
                    child._script["session/start"] = [
                        _msp_test_ok(
                            3, _msp_worker_start_result(
                                _MSP_WORKER_SID_A, ws, **over))]
                    with self.assertRaises(muse.MspError) as cm:
                        muse.msp_worker_session_start(
                            handle, workspace_root=ws,
                            timeout=10)
                    self.assertEqual(cm.exception.code, code,
                                     name)
                    rec = muse._MSP_WORKERS[handle]
                    self.assertIsNone(
                        rec.get("worker_session"), name)
                finally:
                    muse.msp_worker_close(handle, reason="t")

    def test_close_before_ack_refuses_resurrection(self):
        handle, child = self._bootstrap()
        try:
            ws = self._workspace()
            os.write(child._out_w, (
                b'{"jsonrpc":"2.0","method":"session/closed",'
                b'"params":{"reason":"done","sessionId":"'
                + _MSP_WORKER_SID_A.encode("ascii") + b'",'
                b'"viewCursor":null}}\n'))
            child._script["session/start"] = [_msp_test_ok(
                3, _msp_worker_start_result(
                    _MSP_WORKER_SID_A, ws))]
            with self.assertRaises(muse.MspError) as cm:
                muse.msp_worker_session_start(
                    handle, workspace_root=ws, timeout=10)
            self.assertEqual(cm.exception.code,
                             "session-closed-before-ack")
            rec = muse._MSP_WORKERS[handle]
            self.assertIsNone(rec.get("worker_session"))
            self.assertEqual(
                [entry["method"] for entry in
                 rec.get("lifecycle", [])],
                ["session/closed"])
        finally:
            muse.msp_worker_close(handle, reason="t")

    def test_listchanged_full_object_binds(self):
        # session/listChanged carries the FULL Session object
        # (params.session), never a top-level sessionId. A
        # sid-only object is not a schema-valid Session: the
        # pre-bind stash records the sid for the ledger, and the
        # reconcile step validates the real Session fields before
        # the start ack binds. Here the full object verifies, so
        # the same-pipe start binds normally.
        handle, child = self._bootstrap()
        try:
            ws = self._workspace()
            os.write(child._out_w, (
                b'{"jsonrpc":"2.0","method":"session/listChanged",'
                b'"params":{"session":'
                + json.dumps(_msp_worker_session_doc(
                    _MSP_WORKER_SID_A, ws)).encode("utf-8")
                + b"}}\n"))
            ledger = self._poll_ledger(handle, 1)
            self.assertEqual(ledger[0]["method"],
                             "session/listChanged")
            self.assertEqual(ledger[0]["session_id"],
                             _MSP_WORKER_SID_A)
            child._script["session/start"] = [_msp_test_ok(
                3, _msp_worker_start_result(
                    _MSP_WORKER_SID_A, ws))]
            bound = muse.msp_worker_session_start(
                handle, workspace_root=ws, timeout=10)
            self.assertEqual(bound["id"], _MSP_WORKER_SID_A)
            rec = muse._MSP_WORKERS[handle]
            self.assertEqual(rec.get("lifecycle_pending"), [])
        finally:
            muse.msp_worker_close(handle, reason="t")

    def test_listchanged_sid_only_is_malformed_at_reconcile(self):
        # A sid-only "Session" cannot verify its real fields: the
        # reconcile step refuses it as malformed instead of
        # binding on the sid alone.
        handle, child = self._bootstrap()
        try:
            ws = self._workspace()
            os.write(child._out_w, (
                b'{"jsonrpc":"2.0","method":"session/listChanged",'
                b'"params":{"session":{"sessionId":"'
                + _MSP_WORKER_SID_A.encode("ascii")
                + b'"}}}\n'))
            self._poll_ledger(handle, 1)
            child._script["session/start"] = [_msp_test_ok(
                3, _msp_worker_start_result(
                    _MSP_WORKER_SID_A, ws))]
            with self.assertRaises(muse.MspError) as cm:
                muse.msp_worker_session_start(
                    handle, workspace_root=ws, timeout=10)
            self.assertEqual(cm.exception.code, "malformed")
            rec = muse._MSP_WORKERS[handle]
            self.assertIsNone(rec.get("worker_session"))
            # The failed attempt is non-rebindable.
            with self.assertRaises(muse.MspError) as cm2:
                muse.msp_worker_session_start(
                    handle, workspace_root=ws, timeout=10)
            self.assertEqual(cm2.exception.code,
                             "session-closed")
        finally:
            muse.msp_worker_close(handle, reason="t")

    def test_lifecycle_malformed_shapes_break(self):
        for name, payload in (
                ("status-missing-viewcursor",
                 {"sessionId": _MSP_WORKER_SID_A,
                  "status": "idle"}),
                ("closed-missing-reason",
                 {"sessionId": _MSP_WORKER_SID_A,
                  "viewCursor": None}),
                ("listchanged-top-level-id",
                 {"sessionId": _MSP_WORKER_SID_A})):
            method = {"status-missing-viewcursor":
                      "session/statusChanged",
                      "closed-missing-reason":
                      "session/closed",
                      "listchanged-top-level-id":
                      "session/listChanged"}[name]
            with self.subTest(name=name):
                handle, child = self._bootstrap()
                try:
                    os.write(child._out_w, (
                        json.dumps(
                            {"jsonrpc": "2.0", "method": method,
                             "params": payload}).encode("utf-8")
                        + b"\n"))
                    start = time.monotonic()
                    while time.monotonic() - start < 10:
                        rec = muse._MSP_WORKERS.get(handle)
                        if rec is not None and rec.get("broken"):
                            break
                        time.sleep(0.01)
                    rec = muse._MSP_WORKERS[handle]
                    self.assertEqual(rec.get("broken"),
                                     "malformed", name)
                finally:
                    muse.msp_worker_close(handle, reason="t")

    def test_session_read_notloaded_unbinds_without_rebind(self):
        handle, child = self._bootstrap()
        try:
            ws = self._workspace()
            child._script["session/start"] = [_msp_test_ok(
                3, _msp_worker_start_result(
                    _MSP_WORKER_SID_A, ws))]
            muse.msp_worker_session_start(
                handle, workspace_root=ws, timeout=10)
            child._script["session/read"] = [_msp_test_ok(
                4, _msp_worker_read_result(
                    _MSP_WORKER_SID_A, ws,
                    status="notLoaded"))]
            with self.assertRaises(muse.MspError) as cm:
                muse.msp_worker_session_read(handle,
                                             timeout=10)
            self.assertEqual(cm.exception.code,
                             "session-not-loaded")
            rec = muse._MSP_WORKERS[handle]
            self.assertIsNone(rec.get("worker_session"))
            with self.assertRaises(muse.MspError) as cm2:
                muse.msp_worker_session_start(
                    handle, workspace_root=ws, timeout=10)
            self.assertEqual(cm2.exception.code,
                             "session-closed")
        finally:
            muse.msp_worker_close(handle, reason="t")

    def test_turn_refuses_with_no_wire_write(self):
        handle, child = self._bootstrap()
        try:
            ws = self._workspace()
            child._script["session/start"] = [_msp_test_ok(
                3, _msp_worker_start_result(
                    _MSP_WORKER_SID_A, ws))]
            muse.msp_worker_session_start(
                handle, workspace_root=ws, timeout=10)
            before = len(child.raw_requests)
            with self.assertRaises(muse.MspError) as cm:
                muse.msp_worker_turn(
                    handle, "implement the fix", timeout=10)
            self.assertEqual(cm.exception.code,
                             "capability-unproved")
            self.assertEqual(len(child.raw_requests), before)
            self.assertNotIn(b"turn/start",
                             b"\n".join(child.raw_requests))
            with self.assertRaises(muse.MspError) as cm2:
                muse.msp_worker_turn(handle, "", timeout=10)
            self.assertEqual(cm2.exception.code, "bad-input")
        finally:
            muse.msp_worker_close(handle, reason="t")

    def test_owner_binding_tamper_refuses_before_wire(self):
        handle, child = self._bootstrap()
        try:
            ws = self._workspace()
            child._script["session/start"] = [_msp_test_ok(
                3, _msp_worker_start_result(
                    _MSP_WORKER_SID_A, ws))]
            muse.msp_worker_session_start(
                handle, workspace_root=ws, timeout=10)
            before = len(child.raw_requests)
            rec = muse._MSP_WORKERS[handle]
            real_proc = rec["proc"]
            # Substituted child object refuses even though the
            # fake still answers.
            rec["proc"] = mock.Mock()
            rec["proc"].pid = real_proc.pid
            with self.assertRaises(muse.MspError) as cm:
                muse.msp_worker_session_read(handle,
                                             timeout=10)
            self.assertEqual(cm.exception.code,
                             "proc-replaced")
            self.assertEqual(len(child.raw_requests), before)
            rec["proc"] = real_proc
            # Altered pid refuses.
            rec["child_pid"] = real_proc.pid + 1
            with self.assertRaises(muse.MspError) as cm2:
                muse.msp_worker_session_read(handle,
                                             timeout=10)
            self.assertEqual(cm2.exception.code,
                             "proc-pid-mismatch")
            self.assertEqual(len(child.raw_requests), before)
            rec["child_pid"] = real_proc.pid
            real_nonce = rec["nonce"]
            # Corrupt shape refuses.
            rec["nonce"] = "short"
            with self.assertRaises(muse.MspError) as cm3:
                muse.msp_worker_session_read(handle,
                                             timeout=10)
            self.assertEqual(cm3.exception.code,
                             "nonce-corrupt")
            self.assertEqual(len(child.raw_requests), before)
            # Same-size mutation against the immutable spawn
            # pin refuses.
            rec["nonce"] = "f" * 16
            with self.assertRaises(muse.MspError) as cm3b:
                muse.msp_worker_session_read(handle,
                                             timeout=10)
            self.assertEqual(cm3b.exception.code,
                             "nonce-mismatch")
            self.assertEqual(len(child.raw_requests), before)
            rec["nonce"] = real_nonce
            # Use from a non-owner process refuses.
            with mock.patch.object(muse.os, "getpid",
                                   return_value=1):
                with self.assertRaises(muse.MspError) as cm4:
                    muse.msp_worker_session_read(
                        handle, timeout=10)
            self.assertEqual(cm4.exception.code,
                             "owner-mismatch")
            self.assertEqual(len(child.raw_requests), before)
            # The legitimate child still answers once the
            # binding is intact again.
            child._script["session/read"] = [_msp_test_ok(
                4, _msp_worker_read_result(
                    _MSP_WORKER_SID_A, ws))]
            seen = muse.msp_worker_session_read(
                handle, timeout=10)
            self.assertEqual(seen["session"]["id"],
                             _MSP_WORKER_SID_A)
        finally:
            muse.msp_worker_close(handle, reason="t")

    def test_close_never_signals_substituted_pid(self):
        # A stubborn original child forces the kill path; with a
        # substituted proc in the record, cleanup must target only
        # the originally pinned owned pid.
        handle, child = self._bootstrap(wait_mode="none")
        try:
            ws = self._workspace()
            child._script["session/start"] = [_msp_test_ok(
                3, _msp_worker_start_result(
                    _MSP_WORKER_SID_A, ws))]
            muse.msp_worker_session_start(
                handle, workspace_root=ws, timeout=10)
            rec = muse._MSP_WORKERS[handle]
            # Substitute an unrelated live-looking child object.
            rec["proc"] = mock.Mock()
            rec["proc"].pid = 999999999
            with mock.patch.object(
                    muse, "_kill_owned_group",
                    return_value=True) as sink:
                cleanup = muse.msp_worker_close(
                    handle, reason="t")
            for call in sink.call_args_list:
                self.assertNotEqual(
                    call.args[0] if call.args else call[0][0],
                    999999999)
            pids = [call.args[0] if call.args else call[0][0]
                    for call in sink.call_args_list]
            self.assertIn(_MspScriptedChild.FAKE_PID, pids)
            self.assertNotIn(handle, muse._MSP_WORKERS)
            _ = cleanup
        finally:
            if handle in muse._MSP_WORKERS:
                muse.msp_worker_close(handle, reason="t")

    def test_workspace_symlink_component_refuses(self):
        handle, child = self._bootstrap()
        try:
            # The bootstrap handshake itself wrote exactly three
            # OUTBOUND wire records (initialize request, id-less
            # initialized notification, account/read request):
            # snapshot the baseline bytes/count and require each
            # refused session/start below to add NOTHING. JSON key
            # order is not semantic (sort_keys puts "id" first),
            # so parse every object and read its method member —
            # the same decode the _methods helper uses.
            baseline = list(child.raw_requests)
            self.assertEqual(
                [json.loads(line.decode("utf-8")).get("method")
                 for line in baseline],
                ["initialize", "initialized", "account/read"])
            # A FINAL symlink is not a directory: the params stage
            # honestly refuses it as bad-input before any wire use.
            target = self._workspace("realdir")
            link = os.path.join(self.root, "linkdir")
            os.symlink(target, link)
            with self.assertRaises(muse.MspError) as cm:
                muse.msp_worker_session_start(
                    handle, workspace_root=link, timeout=10)
            self.assertEqual(cm.exception.code, "bad-input")
            self.assertEqual(child.raw_requests, baseline)
            # An INTERMEDIATE symlink component with a real final
            # owned empty dir reaches the canonical ownership
            # proof: realpath differs from the input.
            actual = self._workspace("actual")
            self.assertTrue(os.path.isdir(actual))
            via = os.path.join(self.root, "via")
            os.symlink(self.root, via)
            through = os.path.join(via, "actual")
            with self.assertRaises(muse.MspError) as cm2:
                muse.msp_worker_session_start(
                    handle, workspace_root=through,
                    timeout=10)
            self.assertEqual(cm2.exception.code,
                             "workspace-symlink")
            self.assertEqual(child.raw_requests, baseline)
            rec = muse._MSP_WORKERS[handle]
            self.assertIsNone(rec.get("worker_session"))
            self.assertFalse(rec.get("worker_session_closed"))
        finally:
            muse.msp_worker_close(handle, reason="t")

    def test_workspace_swap_after_bind_refuses_read(self):
        handle, child = self._bootstrap()
        try:
            ws = self._workspace()
            child._script["session/start"] = [_msp_test_ok(
                3, _msp_worker_start_result(
                    _MSP_WORKER_SID_A, ws))]
            muse.msp_worker_session_start(
                handle, workspace_root=ws, timeout=10)
            os.rename(ws, ws + ".old")
            os.mkdir(ws, 0o700)
            before = len(child.raw_requests)
            with self.assertRaises(muse.MspError) as cm:
                muse.msp_worker_session_read(handle,
                                             timeout=10)
            self.assertEqual(cm.exception.code,
                             "workspace-changed")
            self.assertEqual(len(child.raw_requests), before)
            with self.assertRaises(muse.MspError) as cm2:
                muse.msp_worker_hooks(handle, timeout=10)
            self.assertEqual(cm2.exception.code,
                             "workspace-changed")
            self.assertEqual(len(child.raw_requests), before)
        finally:
            muse.msp_worker_close(handle, reason="t")

    def test_prebind_drift_refuses_and_burns_handle(self):
        drifts = (
            ("foreign-started",
             {"sessionId": _MSP_WORKER_SID_B,
              "providerId": "meta",
              "modelId": "muse-spark-1.3",
              "workspaceRoot": "/x", "status": "idle",
              "activeTurnId": None, "turnCount": 0,
              "createdAt": "t", "updatedAt": "t",
              "forkedFrom": None, "path": ""},
             "session-mismatch"),
            ("provider-drift-started",
             {"sessionId": _MSP_WORKER_SID_A,
              "providerId": "other",
              "modelId": "muse-spark-1.3",
              "workspaceRoot": "/x", "status": "idle",
              "activeTurnId": None, "turnCount": 0,
              "createdAt": "t", "updatedAt": "t",
              "forkedFrom": None, "path": ""},
             "provider-mismatch"),
            ("prebind-model-changed", None, "model-changed"),
            ("prebind-notloaded", None, "session-not-loaded"),
        )
        for name, session_obj, code in drifts:
            with self.subTest(name=name):
                handle, child = self._bootstrap()
                try:
                    ws = self._workspace("ws-" + name)
                    if session_obj is not None:
                        session_obj = dict(session_obj)
                        session_obj["workspaceRoot"] = ws
                        os.write(child._out_w, (
                            b'{"jsonrpc":"2.0",'
                            b'"method":"session/started",'
                            b'"params":{"session":'
                            + json.dumps(
                                session_obj).encode("utf-8")
                            + b"}}\n"))
                        # Deterministic: the pre-bind event must
                        # be stashed before the start ack arrives.
                        self._poll_ledger(handle, 1)
                    elif name == "prebind-model-changed":
                        os.write(child._out_w, (
                            b'{"jsonrpc":"2.0",'
                            b'"method":"session/modelChanged",'
                            b'"params":{"sessionId":"'
                            + _MSP_WORKER_SID_A.encode("ascii")
                            + b'"}}\n'))
                    else:
                        os.write(child._out_w, (
                            b'{"jsonrpc":"2.0",'
                            b'"method":"session/statusChanged",'
                            b'"params":{"sessionId":"'
                            + _MSP_WORKER_SID_A.encode("ascii")
                            + b'","status":"notLoaded",'
                            b'"viewCursor":null}}\n'))
                    # The pre-bind event must be stashed before
                    # the start ack arrives; polling the ledger
                    # makes the refusal deterministic.
                    self._poll_ledger(handle, 1)
                    child._script["session/start"] = [
                        _msp_test_ok(
                            3, _msp_worker_start_result(
                                _MSP_WORKER_SID_A, ws))]
                    with self.assertRaises(muse.MspError) as cm:
                        muse.msp_worker_session_start(
                            handle, workspace_root=ws,
                            timeout=10)
                    self.assertEqual(cm.exception.code, code,
                                     name)
                    rec = muse._MSP_WORKERS[handle]
                    self.assertIsNone(
                        rec.get("worker_session"), name)
                    # The failed attempt is non-rebindable on
                    # this handle: no duplicate start.
                    with self.assertRaises(muse.MspError) as cm2:
                        muse.msp_worker_session_start(
                            handle, workspace_root=ws,
                            timeout=10)
                    self.assertEqual(cm2.exception.code,
                                     "session-closed", name)
                finally:
                    muse.msp_worker_close(handle, reason="t")

    def test_prebind_overflow_fails_closed(self):
        handle, child = self._bootstrap()
        try:
            rec = muse._MSP_WORKERS[handle]
            line = (b'{"jsonrpc":"2.0",'
                    b'"method":"session/statusChanged",'
                    b'"params":{"sessionId":"'
                    + _MSP_WORKER_SID_A.encode("ascii")
                    + b'","status":"idle",'
                    b'"viewCursor":null}}\n')
            for _ in range(muse._MSP_WORKER_LIFECYCLE_MAX + 1):
                muse._dispatch_worker_inbox(rec, line)
            self.assertEqual(rec.get("broken"),
                             "lifecycle-overflow")
        finally:
            muse.msp_worker_close(handle, reason="t")

    def test_postbind_lifecycle_breaks_and_unbinds(self):
        handle, child = self._bootstrap()
        try:
            ws = self._workspace()
            child._script["session/start"] = [_msp_test_ok(
                3, _msp_worker_start_result(
                    _MSP_WORKER_SID_A, ws))]
            muse.msp_worker_session_start(
                handle, workspace_root=ws, timeout=10)
            rec = muse._MSP_WORKERS[handle]
            # Foreign session id breaks.
            muse._dispatch_worker_inbox(
                rec, (b'{"jsonrpc":"2.0",'
                      b'"method":"session/statusChanged",'
                      b'"params":{"sessionId":"'
                      + _MSP_WORKER_SID_B.encode("ascii")
                      + b'","status":"idle",'
                      b'"viewCursor":null}}\n'))
            self.assertEqual(rec.get("broken"),
                             "session-mismatch")
        finally:
            muse.msp_worker_close(handle, reason="t")
        handle, child = self._bootstrap()
        try:
            ws = self._workspace()
            child._script["session/start"] = [_msp_test_ok(
                3, _msp_worker_start_result(
                    _MSP_WORKER_SID_A, ws))]
            muse.msp_worker_session_start(
                handle, workspace_root=ws, timeout=10)
            rec = muse._MSP_WORKERS[handle]
            # Model change under a bound session breaks.
            muse._dispatch_worker_inbox(
                rec, (b'{"jsonrpc":"2.0",'
                      b'"method":"session/modelChanged",'
                      b'"params":{"sessionId":"'
                      + _MSP_WORKER_SID_A.encode("ascii")
                      + b'"}}\n'))
            self.assertEqual(rec.get("broken"), "model-changed")
        finally:
            muse.msp_worker_close(handle, reason="t")
        handle, child = self._bootstrap()
        try:
            ws = self._workspace()
            child._script["session/start"] = [_msp_test_ok(
                3, _msp_worker_start_result(
                    _MSP_WORKER_SID_A, ws))]
            muse.msp_worker_session_start(
                handle, workspace_root=ws, timeout=10)
            rec = muse._MSP_WORKERS[handle]
            # Close unbinds and blocks rebind on this handle.
            muse._dispatch_worker_inbox(
                rec, (b'{"jsonrpc":"2.0","method":"session/closed",'
                      b'"params":{"reason":"done","sessionId":"'
                      + _MSP_WORKER_SID_A.encode("ascii")
                      + b'","viewCursor":null}}\n'))
            self.assertIsNone(rec.get("worker_session"))
            self.assertTrue(rec.get("worker_session_closed"))
            self.assertIsNone(rec.get("broken"))
            with self.assertRaises(muse.MspError) as cm:
                muse.msp_worker_session_start(
                    handle, workspace_root=ws, timeout=10)
            self.assertEqual(cm.exception.code,
                             "session-closed")
        finally:
            muse.msp_worker_close(handle, reason="t")

    def _accept_rec(self, session_id, closed=False,
                    broken=None):
        return {"worker_session":
                ({"id": session_id} if session_id else None),
                "worker_session_closed": closed,
                "broken": broken,
                "break_lock": threading.Lock()}

    def test_accept_read_guards_live_binding(self):
        ws = "/owned-fixture-ws"
        seen = muse.parse_msp_session_read(
            _msp_worker_read_result(_MSP_WORKER_SID_A, ws),
            ws, _MSP_WORKER_SID_A)
        bound = {"id": _MSP_WORKER_SID_A,
                 "workspace_root": ws,
                 "workspace_dev": 11, "workspace_ino": 22}
        # Positive: pins carried, binding updated.
        rec = self._accept_rec(_MSP_WORKER_SID_A)
        out = muse._worker_accept_read(rec, bound, seen)
        self.assertIs(out, seen)
        self.assertEqual(rec["worker_session"]["id"],
                         _MSP_WORKER_SID_A)
        self.assertEqual(
            (rec["worker_session"]["workspace_dev"],
             rec["worker_session"]["workspace_ino"]),
            (11, 22))
        # Closed during flight: stale result discarded.
        rec = self._accept_rec(None, closed=True)
        with self.assertRaises(muse.MspError) as cm:
            muse._worker_accept_read(rec, bound, seen)
        self.assertEqual(cm.exception.code, "session-closed")
        self.assertIsNone(rec.get("worker_session"))
        # Binding changed during flight: no resurrection.
        rec = self._accept_rec(_MSP_WORKER_SID_B)
        with self.assertRaises(muse.MspError) as cm2:
            muse._worker_accept_read(rec, bound, seen)
        self.assertEqual(cm2.exception.code,
                         "session-mismatch")
        self.assertEqual(rec["worker_session"]["id"],
                         _MSP_WORKER_SID_B)
        # Host broke during flight: break code preserved.
        rec = self._accept_rec(_MSP_WORKER_SID_A,
                               broken="model-changed")
        with self.assertRaises(muse.MspError) as cm3:
            muse._worker_accept_read(rec, bound, seen)
        self.assertEqual(cm3.exception.code, "model-changed")
        # notLoaded unbinds without rebind.
        not_loaded = muse.parse_msp_session_read(
            _msp_worker_read_result(
                _MSP_WORKER_SID_A, ws, status="notLoaded"),
            ws, _MSP_WORKER_SID_A)
        rec = self._accept_rec(_MSP_WORKER_SID_A)
        with self.assertRaises(muse.MspError) as cm4:
            muse._worker_accept_read(rec, bound, not_loaded)
        self.assertEqual(cm4.exception.code,
                         "session-not-loaded")
        self.assertIsNone(rec.get("worker_session"))
        self.assertTrue(rec.get("worker_session_closed"))

    def test_close_corrupt_original_refuses_without_signals(self):
        # Both the working pointer and the recorded original are
        # untrustworthy (ref pid no longer matches the spawn pin):
        # cleanup must release only owned-known streams and never
        # signal or reap, while still invalidating the handle.
        handle, _child = self._bootstrap()
        try:
            rec = muse._MSP_WORKERS[handle]
            rec["proc"] = mock.Mock()
            rec["proc"].pid = 999999999
            rec["_proc_ref"].pid = 12345
            with mock.patch.object(
                    muse, "_kill_owned_group",
                    return_value=True) as sink:
                with self.assertRaises(muse.MspError) as cm:
                    muse.msp_worker_close(handle,
                                          reason="t")
            self.assertEqual(cm.exception.code,
                             "proc-replaced")
            sink.assert_not_called()
            self.assertNotIn(handle, muse._MSP_WORKERS)
        finally:
            if handle in muse._MSP_WORKERS:
                muse.msp_worker_close(handle, reason="t")

    def test_close_from_non_owner_refuses_without_signals(self):
        handle, _child = self._bootstrap()
        try:
            with mock.patch.object(
                    muse, "_kill_owned_group",
                    return_value=True) as sink:
                with mock.patch.object(
                        muse.os, "getpid", return_value=1):
                    with self.assertRaises(muse.MspError) as cm:
                        muse.msp_worker_close(handle,
                                              reason="t")
            self.assertEqual(cm.exception.code,
                             "owner-mismatch")
            sink.assert_not_called()
            self.assertNotIn(handle, muse._MSP_WORKERS)
        finally:
            if handle in muse._MSP_WORKERS:
                muse.msp_worker_close(handle, reason="t")

    def test_close_reaps_already_dead_child_without_signals(
            self):
        handle, child = self._bootstrap()
        child.stdin.close()
        self.assertTrue(child.join_responder(timeout=15))
        with mock.patch.object(
                muse, "_kill_owned_group",
                return_value=True) as sink:
            cleanup = muse.msp_worker_close(handle,
                                            reason="t")
        sink.assert_not_called()
        self.assertEqual(cleanup["state"], "clean")
        self.assertEqual(cleanup["returncode"], 0)
        self.assertFalse(cleanup["signalled"])

    def test_spawn_failure_reaps_owned_child(self):
        base = tempfile.mkdtemp(prefix="msp-spawnfail-")
        work = os.path.realpath(base)
        os.chmod(work, 0o700)
        self.addCleanup(shutil.rmtree, work, True)
        root = os.path.realpath(
            tempfile.mkdtemp(prefix="msp-sfroot-"))
        os.chmod(root, 0o700)
        self.addCleanup(shutil.rmtree, root, True)
        real_lstat = os.lstat
        armed = threading.Event()
        canon = root

        def _flaky_lstat(path, *args, **kwargs):
            if armed.is_set() and os.path.normpath(path) \
                    == canon:
                raise OSError("injected post-spawn failure")
            return real_lstat(path, *args, **kwargs)

        child = _MspScriptedChild(script={})
        self.addCleanup(child.force_close)
        killed = []
        wait_calls = []
        real_wait = child.wait

        def _rec_wait(timeout=None):
            wait_calls.append(timeout)
            return real_wait(timeout=timeout)

        def _rec_kill():
            killed.append(True)
            try:
                child.stdin.close()
            except (OSError, ValueError):
                pass

        child.kill = _rec_kill
        child.wait = _rec_wait

        def _spawn(cmd, env, cwd):
            inst = child
            armed.set()
            return inst

        snap = {"path": work + "/bin",
                "real_path": work + "/bin",
                "sha256": hashlib.sha256(b"x").hexdigest(),
                "dev": 0, "ino": 0, "size": 1}
        with mock.patch.object(
                muse, "_msp_resolve_snapshot",
                return_value=dict(snap)), \
            mock.patch.object(
                muse, "_msp_build_wrapper",
                return_value={"argv": ["sandbox-exec"],
                              "policy": {"path": "p",
                                         "sha256": "s",
                                         "size": 1,
                                         "dev": 0,
                                         "ino": 0}}), \
            mock.patch.object(
                muse, "_msp_spawn_process", autospec=True,
                side_effect=_spawn), \
            mock.patch.object(
                muse.os, "lstat", side_effect=_flaky_lstat):
            with self.assertRaises(muse.MspError) as cm:
                muse.spawn_msp_worker(root)
        self.assertEqual(cm.exception.code, "unreadable")
        self.assertEqual(killed, [True])
        self.assertEqual(wait_calls, [10])
        self.assertEqual(muse._MSP_WORKERS, {})
        self.assertTrue(child.join_responder(timeout=15))

    def test_viewcursor_absent_is_malformed(self):
        handle, _child = self._bootstrap()
        try:
            rec = muse._MSP_WORKERS[handle]
            # Absent viewCursor key refuses (missing is not
            # explicit null).
            muse._dispatch_worker_inbox(
                rec, (b'{"jsonrpc":"2.0",'
                      b'"method":"session/statusChanged",'
                      b'"params":{"sessionId":"'
                      + _MSP_WORKER_SID_A.encode("ascii")
                      + b'","status":"idle"}}\n'))
            self.assertEqual(rec.get("broken"), "malformed")
        finally:
            muse.msp_worker_close(handle, reason="t")

    def test_uuid_variant_is_checked(self):
        bad = ("0193e0f0-7a2b-7c3d-0e4f-5a6b7c8d9e0f")
        with self.assertRaises(muse.MspError) as cm:
            muse._msp_check_uuid(bad, "session id", version="7")
        self.assertEqual(cm.exception.code, "malformed")

    def test_read_rejects_scalar_history(self):
        handle, _child = self._bootstrap()
        try:
            ws = self._workspace()
            good = _msp_worker_read_result(_MSP_WORKER_SID_A,
                                           ws)
            bad_items = dict(good)
            bad_items["history"] = {"items": "x", "mode": "m",
                                    "snapshot": None}
            with self.assertRaises(muse.MspError) as cm:
                muse.parse_msp_session_read(
                    bad_items, ws, _MSP_WORKER_SID_A)
            self.assertEqual(cm.exception.code, "malformed")
            bad_snap = dict(good)
            bad_snap["history"] = {"items": [], "mode": "m",
                                   "snapshot": "x"}
            with self.assertRaises(muse.MspError) as cm2:
                muse.parse_msp_session_read(
                    bad_snap, ws, _MSP_WORKER_SID_A)
            self.assertEqual(cm2.exception.code, "malformed")
        finally:
            muse.msp_worker_close(handle, reason="t")


if __name__ == "__main__":
    unittest.main()
