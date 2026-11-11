"""Slice 298: repair turn through the broker with per-attempt self-proof.

E41 proved a contained exec turn through the allowlisted broker
completes with provider_model muse-spark-1.3-contributor,
source=login, transport=proxy and no network denial. This
slice puts that shape in the product: exec_source_repair runs
under a repair-broker policy (subscription-broker text plus
exactly the write rule for one derived worktree), and each
attempt proves itself from its own stream, owned trace and
broker log before its output is trusted.

Failing-first: every test here fails on the 297c tree (no
repair policy builder, no transport reader, no broker
lifecycle or proof gate in exec) and passes with the slice.
The production-refusal test is a characterization pin: it
passes before and after.
"""

import hashlib
import json
import os
import socket
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__),
                               "..", "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_296_broker import _EchoUpstream  # noqa: E402
from openmainty import derived_fix as derived_fix_mod  # noqa: E402
from openmainty import mspbroker as mspbroker_mod  # noqa: E402
from openmainty import muse as muse_mod  # noqa: E402
from openmainty import ncontain  # noqa: E402
from openmainty import preflight as pf  # noqa: E402

FAKE_HOME = "/Users/qa-owner"
FAKE_PORT = 54321
HOLE_LINE = ('(allow network-outbound (remote tcp '
             '"localhost:54321"))')
PINNED_MODEL = "muse-spark-1.3"


def _roots():
    root_tmp = tempfile.TemporaryDirectory(prefix="msp298-")
    root = os.path.realpath(root_tmp.name)
    os.chmod(root, 0o700)
    return root_tmp, root


class _FakeBroker(object):
    """Recording broker double behind the _BROKER_CLASS seam."""

    instances = []

    def __init__(self, allow=None):
        type(self).instances.append(self)
        self.allow_arg = allow
        self._allow = tuple(allow) if allow is not None \
            else ()
        self.started = False
        self.stopped = False
        self.scripted_log = []
        self.scripted_stop = {"stopped": True, "threads_alive": []}

    def start(self):
        self.started = True
        return FAKE_PORT

    def stop(self):
        self.stopped = True
        return dict(self.scripted_stop)

    def log(self):
        return [dict(entry) for entry in self.scripted_log]


class _FakeProc(object):
    pid = 424243
    returncode = 0

    def __init__(self, returncode=0):
        self.returncode = returncode

    def wait(self, timeout=None):
        return self.returncode


def _usage_stream(model_id=PINNED_MODEL, provider_model=None):
    # Grammar-valid exec stdout: the configured-model record on
    # the observed exec.usage stream. parse_usage reads the
    # contributor model off payload details facets on any
    # record; no E43b record carries facets (36 records, 0
    # kindless, no details key observed), so the facet rides
    # this record to drive the real parser through a valid
    # shape instead of a kindless object. No product guard
    # exception exists for fixtures.
    payload = {"kind": "run_model_configured",
               "model_id": model_id}
    if provider_model is not None:
        payload["details"] = {"facets": [
            {"detail": {"model": provider_model}}]}
    return (json.dumps(
        {"record_type": "event",
         "payload_type": "run.model.configured",
         "payload": payload,
         "stream": {"kind": "exec.usage"}}) + "\n").encode("utf-8")


def _proved_stream():
    # Grammar-valid exec stdout carrying the one observed
    # tool: configured model plus a write_file tool_result.
    # Succeeds end to end only when the raw child bytes reach
    # the proof (a decoded-text handoff refuses every stream).
    return ("\n".join([
        json.dumps(
            {"record_type": "event",
             "payload_type": "run.model.configured",
             "payload": {"kind": "run_model_configured",
                         "model_id": PINNED_MODEL,
                         "details": {"facets": [{"detail": {
                             "model": (
                                 "muse-spark-1.3-contributor")}}]}},
             "stream": {"kind": "exec.usage"}}),
        json.dumps(
            {"record_type": "event",
             "payload_type": "tool.result",
             "payload": {"kind": "tool_result",
                         "correlation_facts": {
                             "outcome": "success",
                             "tool_name": "write_file"},
                         "edit_facts": {"added": 1,
                                        "tool_name": "write_file"}},
             "stream": {"kind": "exec.usage"}}),
    ]) + "\n").encode("utf-8")


def _allowed_entry(target="api.meta.ai:443"):
    return {"method": "CONNECT", "target": target,
            "decision": "allowed", "header_names": ["host"],
            "bytes_up": 100, "bytes_down": 200,
            "at_open": 1.0, "at_close": 2.0}


class RepairBrokerPolicyCase(unittest.TestCase):
    def test_shape_is_broker_plus_workdir_lines(self):
        _keep, root = _roots()
        _keep2, outside = _roots()
        try:
            other = os.path.join(outside, "w")
            os.mkdir(other, 0o700)
            sub = ncontain.build_subscription_broker_policy_text(
                root, "/usr/bin/false", FAKE_HOME, FAKE_PORT)
            got = ncontain.build_repair_broker_policy_text(
                root, "/usr/bin/false", FAKE_HOME, FAKE_PORT,
                other)
            write_line = ('(allow file-write* (subpath "%s"))'
                          % other)
            read_line = ('(allow file-read* (subpath "%s"))'
                         % other)
            deny_line = ncontain._repair_git_deny_line(other)
            self.assertEqual(got, sub + write_line + "\n"
                             + read_line + "\n"
                             + deny_line + "\n")
            self.assertIn("(deny network*)", got)
            self.assertNotIn("*:443", got)
        finally:
            _keep.cleanup()
            _keep2.cleanup()

    def test_read_rule_skipped_when_base_covers_workdir(self):
        _keep, root = _roots()
        try:
            work = os.path.join(root, "work")
            os.mkdir(work, 0o700)
            sub = ncontain.build_subscription_broker_policy_text(
                root, "/usr/bin/false", FAKE_HOME, FAKE_PORT)
            got = ncontain.build_repair_broker_policy_text(
                root, "/usr/bin/false", FAKE_HOME, FAKE_PORT,
                work)
            write_line = ('(allow file-write* (subpath "%s"))'
                          % work)
            deny_line = ncontain._repair_git_deny_line(work)
            self.assertEqual(got, sub + write_line + "\n"
                             + deny_line + "\n")
        finally:
            _keep.cleanup()

    def test_no_write_rule_outside_the_workdir(self):
        _keep, root = _roots()
        _keep2, outside = _roots()
        try:
            other = os.path.join(outside, "w")
            os.mkdir(other, 0o700)
            sub = ncontain.build_subscription_broker_policy_text(
                root, "/usr/bin/false", FAKE_HOME, FAKE_PORT)
            got = ncontain.build_repair_broker_policy_text(
                root, "/usr/bin/false", FAKE_HOME, FAKE_PORT,
                other)
            def _write_allows(text):
                return {ln for ln in text.splitlines()
                        if ln.startswith("(allow")
                        and "file-write" in ln}
            new = _write_allows(got) - _write_allows(sub)
            self.assertEqual(
                new, {'(allow file-write* (subpath "%s"))'
                      % other})
        finally:
            _keep.cleanup()
            _keep2.cleanup()

    def test_refused_workdirs(self):
        _keep, root = _roots()
        try:
            home = os.path.join(root, "home")
            os.makedirs(os.path.join(
                home, ".config", "muse"), 0o700)
            os.makedirs(os.path.join(home, ".muse"), 0o700)
            os.makedirs(os.path.join(
                home, ".local", "share", "muse"), 0o700)
            ok = os.path.join(root, "ok")
            os.mkdir(ok, 0o700)
            link = os.path.join(root, "link")
            os.symlink(ok, link)
            cases = [
                ("missing", os.path.join(root, "nope")),
                ("home", home),
                ("root", "/"),
                ("contains-config",
                 os.path.join(home, ".config")),
                ("inside-config",
                 os.path.join(home, ".config", "muse",
                              "sub")),
                ("inside-dotmuse",
                 os.path.join(home, ".muse", "sub")),
                ("inside-share",
                 os.path.join(home, ".local", "share",
                              "muse", "sub")),
                ("symlink", link),
                ("relative", "rel/path"),
                ("nul", ok + "\x00"),
            ]
            for name, work in cases:
                with self.subTest(name=name):
                    if name == "inside-config":
                        os.makedirs(work, exist_ok=True)
                    if name in ("inside-dotmuse",
                                "inside-share"):
                        os.makedirs(work, exist_ok=True)
                    with self.assertRaises(
                            ncontain.ContainmentError):
                        ncontain.build_repair_broker_policy_text(
                            root, "/usr/bin/false", home,
                            FAKE_PORT, work)
        finally:
            _keep.cleanup()

    def test_proof_refuses_tamper(self):
        _keep, root = _roots()
        _keep2, outside = _roots()
        try:
            other = os.path.join(outside, "w")
            os.mkdir(other, 0o700)
            sub = ncontain.build_subscription_broker_policy_text(
                root, "/usr/bin/false", FAKE_HOME, FAKE_PORT)
            good = ncontain.build_repair_broker_policy_text(
                root, "/usr/bin/false", FAKE_HOME, FAKE_PORT,
                other)
            extra = good.splitlines()[-3:]
            ncontain._prove_repair_plus(good, sub, extra)
            dropped = "\n".join(
                good.splitlines()[:-1]) + "\n"
            with self.assertRaises(
                    ncontain.ContainmentError):
                ncontain._prove_repair_plus(
                    dropped, sub, extra)
            widened = good + ('(allow file-write* (subpath '
                              '"/tmp/evil"))\n')
            with self.assertRaises(
                    ncontain.ContainmentError):
                ncontain._prove_repair_plus(
                    widened, sub, extra)
        finally:
            _keep2.cleanup()
            _keep.cleanup()


class TransportTraceReaderCase(unittest.TestCase):
    def _trace_dir(self, lines, name="t.log"):
        tmp = tempfile.TemporaryDirectory(prefix="msp298tr-")
        path = os.path.join(tmp.name, "data", "muse",
                            "local-tracing")
        os.makedirs(path, 0o700)
        with open(os.path.join(path, name), "w",
                  encoding="utf-8") as fh:
            fh.write("".join(lines))
        return tmp

    def test_proxy_seen(self):
        tmp = self._trace_dir([
            'event="endpoint_transport.resolve" '
            'transport=proxy reason=startup\n',
            'event="provider_transport.build" '
            'transport=proxy auth=bearer\n',
        ])
        try:
            got, reason = pf._read_worker_transport_trace(
                tmp.name)
            self.assertIsNone(reason)
            self.assertEqual(got["proxy"], 2)
            self.assertEqual(got["direct"], 0)
        finally:
            tmp.cleanup()

    def test_direct_only_is_not_proxy(self):
        tmp = self._trace_dir([
            'event="endpoint_transport.resolve" '
            'transport=direct reason=startup\n',
        ])
        try:
            got, reason = pf._read_worker_transport_trace(
                tmp.name)
            self.assertIsNone(reason)
            self.assertEqual(got["proxy"], 0)
            self.assertEqual(got["direct"], 1)
        finally:
            tmp.cleanup()

    def test_no_lines_is_zero_not_missing(self):
        tmp = self._trace_dir(['event="other" x=1\n'])
        try:
            got, reason = pf._read_worker_transport_trace(
                tmp.name)
            self.assertIsNone(reason)
            self.assertEqual(got, {"proxy": 0, "direct": 0})
        finally:
            tmp.cleanup()

    def test_too_many_files_refuses(self):
        tmp = tempfile.TemporaryDirectory(prefix="msp298tm-")
        try:
            path = os.path.join(tmp.name, "data")
            os.makedirs(path, 0o700)
            for i in range(
                    pf._CRED_TRACE_FILES_MAX + 1):
                with open(os.path.join(
                        path, "f%03d.log" % i), "w") as fh:
                    fh.write("x\n")
            got, reason = pf._read_worker_transport_trace(
                tmp.name)
            self.assertIsNone(got)
            self.assertEqual(reason, "trace-too-many-files")
        finally:
            tmp.cleanup()

    def test_secret_token_never_kept(self):
        secret = "sk-fake-298-secret-token-abcdef"
        tmp = self._trace_dir([
            'event="endpoint_transport.resolve" '
            'transport=proxy token=%s\n' % secret,
        ])
        try:
            got, reason = pf._read_worker_transport_trace(
                tmp.name)
            self.assertIsNone(reason)
            self.assertEqual(got["proxy"], 1)
            self.assertNotIn(secret, repr(got))
        finally:
            tmp.cleanup()


class ExecBrokerLifecycleCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(
            prefix="msp298ex-")
        self.home = os.path.join(self.tmp.name, "home")
        os.mkdir(self.home, 0o700)
        self.work = os.path.join(self.tmp.name, "work")
        os.mkdir(self.work, 0o700)
        _FakeBroker.instances = []
        self._env = mock.patch.dict(os.environ)
        self._env.start()
        os.environ.pop("META_API_KEY", None)
        os.environ.pop("MUSE_CONTRIBUTOR_MODE", None)
        self.addCleanup(self._env.stop)
        self.addCleanup(self.tmp.cleanup)

    def _admitted(self, capture, returncode=0,
                  popen_side_effect=None, broker_log=None,
                  cred=({"source": "login",
                         "masks_login": "false"}, None),
                  transport=({"proxy": 1, "direct": 0},
                             None)):
        snap = {"path": "/owned/fixture/muse",
                "real_path": "/owned/fixture/muse",
                "sha256": "f" * 64, "dev": 1, "ino": 2,
                "size": 3}
        broker_patch = mock.patch.object(
            muse_mod, "_BROKER_CLASS", _FakeBroker)
        broker_patch.start()
        self.addCleanup(broker_patch.stop)
        for name, patcher in (
                ("admission", mock.patch.object(
                    pf, "check_target_authority",
                    return_value={"authorized": True,
                                  "kind": "target"})),
                ("containment", mock.patch.object(
                    pf, "native_containment",
                    return_value=None)),
                # Slice 303: the self-test hook is unconditional in
                # the product, so these broker-lifecycle fixtures
                # route it through its own seam with a passing
                # record; the self-test itself is covered by
                # test_303_native_selftest. Slice 304a: the record
                # must also pass the product validator, so the stub
                # carries the expected control names and the exact
                # bindings of THIS fixture's wrapper/policy/snap;
                # the pre-spawn revalidation (which stats a real
                # policy file these fixtures never create) is
                # stubbed to pass at the same seam. Assertions
                # unchanged.
                ("selftest", mock.patch.object(
                    muse_mod, "_native_selftest",
                    return_value={
                        "passed": True,
                        "failed_control": None,
                        "controls": [
                            {"name": n, "status": "passed"}
                            for n in muse_mod
                            ._EXPECTED_SELFTEST_CONTROLS],
                        "policy_sha256": "a" * 64,
                        "policy_size": 1,
                        "policy_dev": 1, "policy_ino": 1,
                        "sandbox_exec": "/usr/bin/sandbox-exec",
                        "pinned_sha256": "f" * 64})),
                ("revalidate", mock.patch.object(
                    muse_mod, "_revalidate_pre_spawn",
                    return_value=(True, ""))),
                ("resolve", mock.patch.object(
                    muse_mod, "resolve_pinned_muse",
                    return_value=dict(snap))),
                ("resnapshot", mock.patch.object(
                    muse_mod, "_snapshot_binary",
                    return_value=dict(snap))),
                ("wrapper", mock.patch.object(
                    muse_mod, "_msp_build_wrapper",
                    return_value={
                        "argv": ["/usr/bin/sandbox-exec",
                                 "-f", "/owned/policy",
                                 "/owned/fixture/muse"],
                        "policy": {"path": "/owned/policy",
                                   "sha256": "a" * 64,
                                   "size": 1, "dev": 1,
                                   "ino": 1,
                                   "sandbox_exec":
                                   "/usr/bin/sandbox-exec"}})),
                ("capture", mock.patch.object(
                    muse_mod, "_communicate_bounded",
                    return_value=capture)),
                ("spawn", mock.patch.object(
                    muse_mod.subprocess, "Popen",
                    side_effect=popen_side_effect
                    if popen_side_effect is not None
                    else [ _FakeProc(returncode)])),
                ("cred", mock.patch(
                    "openmainty.preflight."
                    "_read_worker_credential_trace",
                    return_value=cred)),
                ("transport", mock.patch(
                    "openmainty.preflight."
                    "_read_worker_transport_trace",
                    return_value=transport)),
                ("sink", mock.patch.object(
                    muse_mod, "_kill_owned_group",
                    return_value=True))):
            patcher.start()
            self.addCleanup(patcher.stop)
        if broker_log is not None:
            orig_start = _FakeBroker.start
            def _start_with_log(self):
                port = orig_start(self)
                self.scripted_log = list(broker_log)
                return port
            _FakeBroker.start = _start_with_log
            self.addCleanup(
                setattr, _FakeBroker, "start", orig_start)
        return _FakeBroker

    def _ok_capture(self, out_b):
        return (out_b, b"", "ok", False, False, True, True)

    def test_broker_stopped_on_success(self):
        self._admitted(
            self._ok_capture(_usage_stream(
                provider_model=(
                    "muse-spark-1.3-contributor"))),
            broker_log=[_allowed_entry()])
        code, out, usage = muse_mod.exec_source_repair(
            "repair prompt", self.work,
            admission_kind="target",
            preflight_home=self.home)
        self.assertEqual(code, 0)
        self.assertEqual(len(_FakeBroker.instances), 1)
        broker = _FakeBroker.instances[0]
        self.assertTrue(broker.started)
        self.assertTrue(broker.stopped)
        self.assertEqual(usage["broker_log"],
                         [_allowed_entry()])
        self.assertEqual(usage["broker_stop"],
                         {"stopped": True,
                          "threads_alive": []})

    def test_broker_stopped_on_timeout(self):
        self._admitted((b"", b"", "timeout", False, False,
                        True, True))
        code, _out, usage = muse_mod.exec_source_repair(
            "repair prompt", self.work, timeout=30,
            admission_kind="target",
            preflight_home=self.home)
        self.assertEqual(code, 124)
        broker = _FakeBroker.instances[0]
        self.assertTrue(broker.stopped)
        self.assertIn("broker_log", usage)
        self.assertIn("broker_stop", usage)

    def test_broker_stopped_on_cancel(self):
        self._admitted((b"", b"", "cancelled", False, False,
                        True, True))
        code, _out, usage = muse_mod.exec_source_repair(
            "repair prompt", self.work,
            admission_kind="target",
            preflight_home=self.home)
        self.assertEqual(code, 130)
        broker = _FakeBroker.instances[0]
        self.assertTrue(broker.stopped)
        self.assertIn("broker_log", usage)

    def test_broker_stopped_on_spawn_failure(self):
        self._admitted(
            self._ok_capture(b""),
            popen_side_effect=OSError("no spawn"))
        code, _out, usage = muse_mod.exec_source_repair(
            "repair prompt", self.work,
            admission_kind="target",
            preflight_home=self.home)
        self.assertEqual(code, 127)
        broker = _FakeBroker.instances[0]
        self.assertTrue(broker.stopped)
        self.assertIn("broker_log", usage)

    def test_policy_failure_stops_broker_no_spawn(self):
        fake_bin = os.path.realpath(
            os.path.join(self.tmp.name, "muse"))
        with open(fake_bin, "wb") as fh:
            fh.write(b"fake-binary-bytes")
        os.chmod(fake_bin, 0o700)
        with open(fake_bin, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        st = os.lstat(fake_bin)
        snap = {"path": fake_bin, "real_path": fake_bin,
                "sha256": digest, "dev": st.st_dev,
                "ino": st.st_ino, "size": st.st_size}
        broker_patch = mock.patch.object(
            muse_mod, "_BROKER_CLASS", _FakeBroker)
        broker_patch.start()
        self.addCleanup(broker_patch.stop)
        for patcher in (
                mock.patch.object(
                    pf, "check_target_authority",
                    return_value={"authorized": True,
                                  "kind": "target"}),
                mock.patch.object(
                    pf, "native_containment",
                    return_value=None),
                mock.patch.object(
                    muse_mod, "resolve_pinned_muse",
                    return_value=dict(snap)),
                mock.patch.object(
                    muse_mod, "_snapshot_binary",
                    return_value=dict(snap))):
            patcher.start()
            self.addCleanup(patcher.stop)
        popen_mock = mock.patch.object(
            muse_mod.subprocess, "Popen",
            side_effect=AssertionError("must not spawn"))
        popen_mock.start()
        self.addCleanup(popen_mock.stop)
        missing = os.path.join(self.tmp.name, "nope")
        code, out, usage = muse_mod.exec_source_repair(
            "repair prompt", missing,
            admission_kind="target",
            preflight_home=self.home)
        self.assertEqual(code, 125)
        self.assertEqual(out, "")
        broker = _FakeBroker.instances[0]
        self.assertTrue(broker.started)
        self.assertTrue(broker.stopped)
        self.assertIn("broker_log", usage)

    def test_production_refuses_before_broker_start(self):
        popen_mock = mock.patch.object(
            muse_mod.subprocess, "Popen",
            side_effect=AssertionError("must not spawn"))
        popen_mock.start()
        self.addCleanup(popen_mock.stop)
        broker_patch = mock.patch.object(
            muse_mod, "_BROKER_CLASS", _FakeBroker)
        broker_patch.start()
        self.addCleanup(broker_patch.stop)
        code, out, usage = muse_mod.exec_source_repair(
            "repair prompt", self.work,
            admission_kind="target",
            preflight_home=self.home)
        self.assertEqual(code, 125)
        self.assertEqual(out, "")
        self.assertEqual(_FakeBroker.instances, [])

    def test_nonzero_exit_passthrough_unchanged(self):
        self._admitted(self._ok_capture(b"boom\n"),
                       returncode=1)
        code, out, usage = muse_mod.exec_source_repair(
            "repair prompt", self.work,
            admission_kind="target",
            preflight_home=self.home)
        self.assertEqual(code, 1)
        self.assertEqual(out, "boom\n")
        self.assertIn("broker_log", usage)


class ExecSelfProofCase(ExecBrokerLifecycleCase):
    def _proof_call(self, **kwargs):
        return muse_mod.exec_source_repair(
            "repair prompt", self.work,
            admission_kind="target",
            preflight_home=self.home, **kwargs)

    def test_contributor_failure_refused(self):
        self._admitted(
            self._ok_capture(_usage_stream(
                provider_model="muse-spark-1.3")),
            broker_log=[_allowed_entry()])
        code, out, usage = self._proof_call()
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(usage.get("code"),
                         "exec-contributor-unproved")

    def test_model_mismatch_refused(self):
        self._admitted(
            self._ok_capture(_usage_stream(
                model_id="other-model",
                provider_model=(
                    "muse-spark-1.3-contributor"))),
            broker_log=[_allowed_entry()])
        code, out, usage = self._proof_call()
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(usage.get("code"),
                         "exec-contributor-unproved")

    def test_credential_failure_refused(self):
        self._admitted(
            self._ok_capture(_usage_stream(
                provider_model=(
                    "muse-spark-1.3-contributor"))),
            broker_log=[_allowed_entry()],
            cred=(None, "credential-status-missing"))
        code, out, usage = self._proof_call()
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(usage.get("code"),
                         "exec-credential-unproved")

    def test_credential_masks_login_refused(self):
        self._admitted(
            self._ok_capture(_usage_stream(
                provider_model=(
                    "muse-spark-1.3-contributor"))),
            broker_log=[_allowed_entry()],
            cred=({"source": "login",
                   "masks_login": "true"}, None))
        code, out, usage = self._proof_call()
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(usage.get("code"),
                         "exec-credential-unproved")

    def test_transport_failure_refused(self):
        self._admitted(
            self._ok_capture(_usage_stream(
                provider_model=(
                    "muse-spark-1.3-contributor"))),
            broker_log=[_allowed_entry()],
            transport=({"proxy": 0, "direct": 1}, None))
        code, out, usage = self._proof_call()
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(usage.get("code"),
                         "exec-transport-unproved")

    def test_broker_denied_refused(self):
        denied = dict(_allowed_entry())
        denied["decision"] = "denied"
        self._admitted(
            self._ok_capture(_usage_stream(
                provider_model=(
                    "muse-spark-1.3-contributor"))),
            broker_log=[denied])
        code, out, usage = self._proof_call()
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(usage.get("code"),
                         "exec-broker-unproved")

    def test_broker_empty_refused(self):
        self._admitted(
            self._ok_capture(_usage_stream(
                provider_model=(
                    "muse-spark-1.3-contributor"))),
            broker_log=[])
        code, out, usage = self._proof_call()
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(usage.get("code"),
                         "exec-broker-unproved")

    def test_broker_wrong_target_refused(self):
        self._admitted(
            self._ok_capture(_usage_stream(
                provider_model=(
                    "muse-spark-1.3-contributor"))),
            broker_log=[_allowed_entry(
                target="evil.example:443")])
        code, out, usage = self._proof_call()
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(usage.get("code"),
                         "exec-broker-unproved")

    def test_proof_reads_child_bytes_not_text(self):
        # Source-aligned seam pin: grammar-valid child bytes
        # through the real exec_source_repair path succeed
        # with every independent leg proven. A decoded-text
        # handoff to the proof would refuse "no readable
        # stream" here instead.
        self._admitted(
            self._ok_capture(_proved_stream()),
            broker_log=[_allowed_entry()])
        code, out, usage = self._proof_call()
        self.assertEqual(code, 0)
        self.assertEqual(usage.get("repair_tools"),
                         ["write_file"])

    def test_kindless_facets_stream_refused(self):
        # The legacy kindless facets object still drives
        # parse_usage to contributor-True, so this pins the
        # tool leg refusing it end to end before any later
        # leg can pass: E43b carries 36 records, 0 kindless.
        first = {
            "record_type": "event",
            "payload_type": "run.model.configured",
            "payload": {"kind": "run_model_configured",
                        "model_id": PINNED_MODEL},
            "stream": {"kind": "exec.usage"}}
        second = {
            "record_type": "event",
            "payload_type": "run.usage",
            "payload": {"details": {"facets": [
                {"detail": {"model":
                            "muse-spark-1.3-contributor"}}]}},
            "stream": {"kind": "exec.usage"}}
        stream = ("\n".join([json.dumps(first),
                             json.dumps(second)])
                  + "\n").encode("utf-8")
        self._admitted(
            self._ok_capture(stream),
            broker_log=[_allowed_entry()])
        code, out, usage = self._proof_call()
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(usage.get("code"),
                         "exec-tool-unproved")

    def test_malformed_child_bytes_refused(self):
        # Bytes the guard cannot parse refuse end to end even
        # though the contributor leg parses: fail closed on
        # the raw bytes, not on decoded text.
        stream = _usage_stream(
            provider_model=(
                "muse-spark-1.3-contributor")) + b"\xff\xfe\n"
        self._admitted(
            self._ok_capture(stream),
            broker_log=[_allowed_entry()])
        code, out, usage = self._proof_call()
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(usage.get("code"),
                         "exec-tool-unproved")


class _CompletesOnStop(object):
    """Broker double whose log completes only after stop."""

    def __init__(self):
        self._stopped = False

    def log(self):
        return [{"method": "CONNECT",
                 "target": "api.meta.ai:443",
                 "decision": "allowed",
                 "bytes_up": 100 if self._stopped else 0,
                 "bytes_down": 200 if self._stopped else 0,
                 "at_open": 1.0,
                 "at_close": 2.0 if self._stopped else None}]

    def stop(self):
        self._stopped = True
        return {"joined": 1, "alive": []}


class HaltOrderCase(unittest.TestCase):
    def test_log_read_after_stop(self):
        final = muse_mod._halt_exec_broker(
            _CompletesOnStop())
        entry = final["broker_log"][0]
        self.assertEqual(entry["at_close"], 2.0)
        self.assertEqual(entry["bytes_up"], 100)
        self.assertEqual(entry["bytes_down"], 200)
        self.assertEqual(final["broker_stop"],
                         {"joined": 1, "alive": []})

    def test_real_broker_open_tunnel_completes(self):
        upstream = _EchoUpstream()
        try:
            broker = mspbroker_mod.ConnectBroker(
                allow=(("127.0.0.1", upstream.port),))
            try:
                port = broker.start()
                client = socket.create_connection(
                    ("127.0.0.1", port), timeout=5)
                try:
                    client.settimeout(5)
                    client.sendall(
                        ("CONNECT 127.0.0.1:%d HTTP/1.1\r\n"
                         "Host: 127.0.0.1\r\n\r\n"
                         % upstream.port).encode("ascii"))
                    resp = client.recv(4096)
                    self.assertIn(b"200", resp)
                    final = muse_mod._halt_exec_broker(
                        broker)
                finally:
                    client.close()
            finally:
                broker.stop()
            entries = [entry
                       for entry in final["broker_log"]
                       if entry["decision"] == "allowed"]
            self.assertEqual(len(entries), 1)
            self.assertIsNotNone(entries[0]["at_close"])
        finally:
            upstream.close()


class ExecFinallyCase(ExecBrokerLifecycleCase):
    def test_unexpected_wrapper_error_stops_broker(self):
        snap = {"path": "/owned/fixture/muse",
                "real_path": "/owned/fixture/muse",
                "sha256": "f" * 64, "dev": 1, "ino": 2,
                "size": 3}
        broker_patch = mock.patch.object(
            muse_mod, "_BROKER_CLASS", _FakeBroker)
        broker_patch.start()
        self.addCleanup(broker_patch.stop)
        for patcher in (
                mock.patch.object(
                    pf, "check_target_authority",
                    return_value={"authorized": True,
                                  "kind": "target"}),
                mock.patch.object(
                    pf, "native_containment",
                    return_value=None),
                mock.patch.object(
                    muse_mod, "resolve_pinned_muse",
                    return_value=dict(snap)),
                mock.patch.object(
                    muse_mod, "_snapshot_binary",
                    return_value=dict(snap)),
                mock.patch.object(
                    muse_mod, "_msp_build_wrapper",
                    side_effect=TypeError("boom"))):
            patcher.start()
            self.addCleanup(patcher.stop)
        with self.assertRaises(TypeError):
            muse_mod.exec_source_repair(
                "repair prompt", self.work,
                admission_kind="target",
                preflight_home=self.home)
        self.assertEqual(len(_FakeBroker.instances), 1)
        broker = _FakeBroker.instances[0]
        self.assertTrue(broker.started)
        self.assertTrue(broker.stopped)


class ProofTightenCase(ExecBrokerLifecycleCase):
    def _proof_call(self):
        return muse_mod.exec_source_repair(
            "repair prompt", self.work,
            admission_kind="target",
            preflight_home=self.home)

    def test_other_contributor_suffix_refused(self):
        self._admitted(
            self._ok_capture(_usage_stream(
                provider_model="other-contributor")),
            broker_log=[_allowed_entry()])
        code, out, usage = self._proof_call()
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(usage.get("code"),
                         "exec-contributor-unproved")

    def test_any_direct_line_refused(self):
        self._admitted(
            self._ok_capture(_usage_stream(
                provider_model=(
                    "muse-spark-1.3-contributor"))),
            broker_log=[_allowed_entry()],
            transport=({"proxy": 1, "direct": 1}, None))
        code, out, usage = self._proof_call()
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(usage.get("code"),
                         "exec-transport-unproved")


class RepairRootCase(ExecBrokerLifecycleCase):
    def test_root_under_preflight_home_recorded(self):
        self._admitted(
            self._ok_capture(_usage_stream(
                provider_model=(
                    "muse-spark-1.3-contributor"))),
            broker_log=[_allowed_entry()])
        code, _out, usage = muse_mod.exec_source_repair(
            "repair prompt", self.work,
            admission_kind="target",
            preflight_home=self.home)
        self.assertEqual(code, 0)
        want = os.path.join(os.path.realpath(self.home),
                            "repair-roots")
        self.assertTrue(usage["repair_root"].startswith(
            want + os.sep))
        self.assertTrue(os.path.isdir(usage["repair_root"]))
        self.assertTrue(os.path.isdir(os.path.join(
            usage["repair_root"], "data")))


class CanonicalWorkdirCase(unittest.TestCase):
    def test_symlinked_home_builds_policy(self):
        real_tmp = tempfile.TemporaryDirectory(
            prefix="msp298real-")
        link_tmp = tempfile.TemporaryDirectory(
            prefix="msp298link-")
        canon_tmp = tempfile.TemporaryDirectory(
            prefix="msp298c-")
        try:
            real = os.path.realpath(real_tmp.name)
            link = os.path.join(
                os.path.realpath(link_tmp.name),
                "homelink")
            os.symlink(real, link)
            canon = os.path.realpath(canon_tmp.name)
            os.chmod(canon, 0o700)
            with mock.patch.dict(
                    os.environ,
                    {"OPENMAINTY_HOME": link}):
                work = derived_fix_mod._expected_work_path(
                    "dfix-" + "ab" * 8)
                self.assertTrue(work.startswith(
                    link + os.sep))
                os.makedirs(os.path.realpath(work),
                            mode=0o700)
                passed = derived_fix_mod.\
                    _canonicalise_workdir(work)
                self.assertEqual(
                    passed, os.path.realpath(work))
                text = ncontain.\
                    build_repair_broker_policy_text(
                        canon, "/usr/bin/false",
                        real, FAKE_PORT, passed)
                self.assertIn("(deny network*)", text)
                with self.assertRaises(
                        ncontain.ContainmentError):
                    ncontain.\
                        build_repair_broker_policy_text(
                            canon, "/usr/bin/false",
                            real, FAKE_PORT, work)
        finally:
            canon_tmp.cleanup()
            link_tmp.cleanup()
            real_tmp.cleanup()

    def test_derived_call_site_canonicalises(self):
        with open(derived_fix_mod.__file__,
                  encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn(
            '_canonicalise_workdir(ws["path"])', src)


if __name__ == "__main__":
    unittest.main()
