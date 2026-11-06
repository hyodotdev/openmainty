"""Slice 296: subscription-broker profile + loopback CONNECT broker.

Gate 2 needs a real allowlist instead of "TCP 443 to any
host". E38 showed serve accepts
-c endpoint_transport.proxy="http://127.0.0.1:PORT" (trace
transport=proxy on both events) and the test proxy saw
CONNECT api.meta.ai:443. This slice puts that shape in the
product: a broker profile whose policy allows egress only
to the loopback broker, and a stdlib CONNECT broker that
tunnels exact allowlisted host:port pairs with no TLS
interception and no value logging.

Failing-first: every test here fails on the 295b tree
(no builder, no module, profile refused) and passes with
the slice.
"""

import importlib
import os
import pwd
import socket
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__),
                               "..", "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from openmainty import muse as muse_mod  # noqa: E402
from openmainty import ncontain  # noqa: E402
from test_273_followups import (  # noqa: E402
    Worker273Mixin, _MspScriptedChild, _msp_test_ok)

FAKE_HOME = "/Users/qa-owner"
FAKE_PORT = 54321
HOLE_LINE = ('(allow network-outbound (remote tcp '
             '"localhost:54321"))')


def _broker_mod():
    return importlib.import_module("openmainty.mspbroker")


def _roots():
    root_tmp = tempfile.TemporaryDirectory(prefix="msp296-")
    root = os.path.realpath(root_tmp.name)
    os.chmod(root, 0o700)
    return root_tmp, root


class BrokerPolicyCase(unittest.TestCase):
    def test_shape_is_base_plus_one_line(self):
        _keep, root = _roots()
        try:
            sub = ncontain.build_subscription_worker_policy_text(
                root, "/usr/bin/false", FAKE_HOME)
            got = ncontain.build_subscription_broker_policy_text(
                root, "/usr/bin/false", FAKE_HOME, FAKE_PORT)
            self.assertEqual(
                got, sub + HOLE_LINE + "\n")
            self.assertIn("(deny network*)", got)
            self.assertNotIn("*:443", got)
        finally:
            _keep.cleanup()

    def test_port_validated(self):
        _keep, root = _roots()
        try:
            for bad in (0, 80, 1023, 65536, -1, "54321",
                        None, True, 54321.0):
                with self.subTest(port=bad):
                    with self.assertRaises(
                            ncontain.ContainmentError):
                        ncontain.build_subscription_broker_policy_text(
                            root, "/usr/bin/false", FAKE_HOME,
                            bad)
            for good in (1024, 8080, 65535):
                with self.subTest(port=good):
                    text = ncontain.\
                        build_subscription_broker_policy_text(
                            root, "/usr/bin/false", FAKE_HOME,
                            good)
                    self.assertIn('"localhost:%d"' % good,
                                  text)
        finally:
            _keep.cleanup()

    def test_no_dns_lines(self):
        _keep, root = _roots()
        try:
            got = ncontain.build_subscription_broker_policy_text(
                root, "/usr/bin/false", FAKE_HOME, FAKE_PORT)
            self.assertNotIn("mDNSResponder", got)
            self.assertNotIn('"/var"', got)
        finally:
            _keep.cleanup()

    def test_proof_refuses_tampering(self):
        _keep, root = _roots()
        try:
            sub = ncontain.build_subscription_worker_policy_text(
                root, "/usr/bin/false", FAKE_HOME)
            good = ncontain.build_subscription_broker_policy_text(
                root, "/usr/bin/false", FAKE_HOME, FAKE_PORT)
            ncontain._prove_broker_plus(good, sub, HOLE_LINE)
            # Extra line.
            with self.assertRaises(
                    ncontain.ContainmentError):
                ncontain._prove_broker_plus(
                    good + "(allow network*)\n", sub,
                    HOLE_LINE)
            # Missing line.
            with self.assertRaises(
                    ncontain.ContainmentError):
                ncontain._prove_broker_plus(
                    sub, sub, HOLE_LINE)
            # Reordered (hole first is impossible by shape,
            # so a swapped tail proves the check instead).
            lines = good.splitlines()
            head, tail = lines[:-1], lines[-1:]
            tampered = "\n".join(
                head[:-1] + tail + head[-1:]) + "\n"
            with self.assertRaises(
                    ncontain.ContainmentError):
                ncontain._prove_broker_plus(
                    tampered, sub, HOLE_LINE)
            # Hole already in base.
            with self.assertRaises(
                    ncontain.ContainmentError):
                ncontain._prove_broker_plus(
                    good, good, HOLE_LINE)
        finally:
            _keep.cleanup()

    def test_older_profiles_byte_identical(self):
        _keep, root = _roots()
        try:
            sub = ncontain.build_subscription_worker_policy_text(
                root, "/usr/bin/false", FAKE_HOME)
            can = ncontain.build_subscription_canary_policy_text(
                root, "/usr/bin/false", FAKE_HOME)
            self.assertNotIn("localhost", sub)
            self.assertNotIn("localhost", can)
            self.assertTrue(can.endswith(
                '(allow file-read-metadata (literal "/var"))'
                "\n"))
            # Rebuilding is stable: the new builder changes
            # nothing about the old ones.
            again = ncontain.\
                build_subscription_canary_policy_text(
                    root, "/usr/bin/false", FAKE_HOME)
            self.assertEqual(can, again)
        finally:
            _keep.cleanup()


class _EchoUpstream:
    """Fake local upstream: accepts one connection, echoes."""

    def __init__(self):
        self.accepted = 0
        self._srv = socket.socket(socket.AF_INET,
                                  socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET,
                             socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(8)
        self._srv.settimeout(5)
        self.port = self._srv.getsockname()[1]
        self._thread = threading.Thread(target=self._run,
                                        daemon=True)
        self._thread.start()

    def _run(self):
        try:
            conn, _peer = self._srv.accept()
        except OSError:
            return
        self.accepted += 1
        conn.settimeout(5)
        try:
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                conn.sendall(data)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self):
        try:
            self._srv.close()
        except OSError:
            pass
        self._thread.join(timeout=10)


def _proxy_request(port, payload, recv_n=4096):
    sock = socket.create_connection(("127.0.0.1", port),
                                    timeout=5)
    sock.settimeout(5)
    try:
        sock.sendall(payload)
        return sock.recv(recv_n)
    finally:
        sock.close()


class BrokerCase(unittest.TestCase):
    def test_loopback_only_bind(self):
        mb = _broker_mod()
        with self.assertRaises(ValueError):
            mb.ConnectBroker(bind="0.0.0.0")
        with self.assertRaises(ValueError):
            mb.ConnectBroker(bind="1.2.3.4")
        broker = mb.ConnectBroker()
        try:
            port = broker.start()
            self.assertTrue(1023 < port < 65536)
        finally:
            broker.stop()

    def test_default_allow_is_meta_443(self):
        mb = _broker_mod()
        self.assertEqual(tuple(mb.DEFAULT_ALLOW),
                         (("api.meta.ai", 443),))

    def test_allowed_host_relays_both_ways(self):
        mb = _broker_mod()
        upstream = _EchoUpstream()
        try:
            broker = mb.ConnectBroker(
                allow=(("127.0.0.1", upstream.port),))
            try:
                port = broker.start()
                sock = socket.create_connection(
                    ("127.0.0.1", port), timeout=5)
                sock.settimeout(5)
                try:
                    sock.sendall(
                        ("CONNECT 127.0.0.1:%d HTTP/1.1\r\n"
                         "Host: 127.0.0.1\r\n\r\n"
                         % upstream.port).encode("ascii"))
                    head = b""
                    while b"\r\n\r\n" not in head:
                        part = sock.recv(4096)
                        if not part:
                            break
                        head += part
                    self.assertTrue(
                        head.startswith(b"HTTP/1.1 200"))
                    sock.sendall(b"ping-relay")
                    self.assertEqual(sock.recv(4096),
                                     b"ping-relay")
                finally:
                    sock.close()
            finally:
                broker.stop()
            log = broker.log()
            self.assertEqual(len(log), 1)
            entry = log[0]
            self.assertEqual(entry["method"], "CONNECT")
            self.assertIn(str(upstream.port),
                          entry["target"])
            self.assertEqual(entry["decision"], "allowed")
            self.assertGreaterEqual(entry["bytes_up"], 10)
            self.assertGreaterEqual(entry["bytes_down"], 10)
            self.assertLessEqual(entry["at_open"],
                                 entry["at_close"])
            self.assertIn("Host", entry["header_names"])
        finally:
            upstream.close()

    def test_denied_host_gets_403_no_upstream(self):
        mb = _broker_mod()
        upstream = _EchoUpstream()
        try:
            broker = mb.ConnectBroker(
                allow=(("127.0.0.1", upstream.port),))
            try:
                port = broker.start()
                resp = _proxy_request(
                    port,
                    b"CONNECT 127.0.0.1:1 HTTP/1.1\r\n"
                    b"Host: 127.0.0.1\r\n\r\n")
                self.assertTrue(resp.startswith(b"HTTP/1.1 403"))
            finally:
                broker.stop()
            self.assertEqual(upstream.accepted, 0)
            log = broker.log()
            self.assertEqual(len(log), 1)
            self.assertEqual(log[0]["decision"], "denied")
        finally:
            upstream.close()

    def test_non_connect_gets_403(self):
        mb = _broker_mod()
        broker = mb.ConnectBroker()
        try:
            port = broker.start()
            resp = _proxy_request(
                port,
                b"GET http://127.0.0.1:1/ HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n\r\n")
            self.assertTrue(resp.startswith(b"HTTP/1.1 403"))
        finally:
            broker.stop()
        log = broker.log()
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["decision"],
                         "denied-not-connect")

    def test_no_value_logging(self):
        mb = _broker_mod()
        secret = "s3cr3t-proxy-password-value"
        broker = mb.ConnectBroker()
        try:
            port = broker.start()
            _proxy_request(
                port,
                ("CONNECT 127.0.0.1:1 HTTP/1.1\r\n"
                 "Host: 127.0.0.1\r\n"
                 "Proxy-Authorization: Basic %s\r\n"
                 "X-Custom: %s\r\n\r\n"
                 % (secret, secret)).encode("ascii"))
        finally:
            broker.stop()
        blob = repr(broker.log())
        self.assertNotIn(secret, blob)
        self.assertIn("Proxy-Authorization",
                      broker.log()[0]["header_names"])

    def test_stop_joins_threads(self):
        mb = _broker_mod()
        broker = mb.ConnectBroker()
        port = broker.start()
        threads = [th for th in threading.enumerate()
                   if getattr(th, "_msp_broker", False)]
        self.assertTrue(threads)
        result = broker.stop()
        self.assertEqual(result["alive"], [])
        for th in threads:
            th.join(timeout=10)
            self.assertFalse(th.is_alive())
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port),
                                     timeout=2)

    def test_open_tunnel_logged_before_stop_returns(self):
        mb = _broker_mod()
        upstream = _EchoUpstream()
        try:
            broker = mb.ConnectBroker(
                allow=(("127.0.0.1", upstream.port),))
            try:
                port = broker.start()
                sock = socket.create_connection(
                    ("127.0.0.1", port), timeout=5)
                sock.settimeout(5)
                try:
                    sock.sendall(
                        ("CONNECT 127.0.0.1:%d HTTP/1.1\r\n"
                         "Host: x\r\n\r\n"
                         % upstream.port).encode("ascii"))
                    head = b""
                    while b"\r\n\r\n" not in head:
                        head += sock.recv(4096)
                    self.assertTrue(
                        head.startswith(b"HTTP/1.1 200"))
                    sock.sendall(b"before-stop")
                    self.assertEqual(sock.recv(4096),
                                     b"before-stop")
                    result = broker.stop()
                    self.assertEqual(result["alive"], [])
                    # The tunnel is still open at stop():
                    # its entry must already be recorded.
                    log = broker.log()
                    self.assertEqual(len(log), 1)
                    self.assertEqual(log[0]["decision"],
                                     "allowed")
                    self.assertGreaterEqual(
                        log[0]["bytes_up"], 11)
                    self.assertGreaterEqual(
                        log[0]["bytes_down"], 11)
                finally:
                    sock.close()
            finally:
                broker.stop()
        finally:
            upstream.close()

    def test_target_reduced_no_secrets(self):
        mb = _broker_mod()
        broker = mb.ConnectBroker()
        try:
            port = broker.start()
            _proxy_request(
                port,
                b"GET http://example.com/path?"
                b"token=topsecret1 HTTP/1.1\r\n"
                b"Host: example.com\r\n\r\n")
            _proxy_request(
                port,
                b"CONNECT user:topsecret2@127.0.0.1:1 "
                b"HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n\r\n")
        finally:
            broker.stop()
        blob = repr(broker.log())
        self.assertNotIn("topsecret1", blob)
        self.assertNotIn("topsecret2", blob)
        self.assertNotIn("/path", blob)
        self.assertNotIn("user:", blob)
        log = broker.log()
        self.assertEqual(len(log), 2)
        self.assertEqual(log[0]["target"], "example.com")
        self.assertEqual(log[1]["target"], "unparsed")
        self.assertEqual(log[1]["decision"], "denied")

    def test_port_must_be_ascii_digits(self):
        mb = _broker_mod()
        broker = mb.ConnectBroker()
        try:
            port = broker.start()
            # "443 " is absent on purpose: a trailing
            # space is the request-line field separator, so
            # 127.0.0.1:443 parses validly.
            for bad in ("+443", " 443", "4_43", "0x1bb"):
                with self.subTest(port=bad):
                    resp = _proxy_request(
                        port,
                        ("CONNECT 127.0.0.1:%s HTTP/1.1\r\n"
                         "Host: x\r\n\r\n" % bad).encode(
                             "ascii"))
                    self.assertTrue(
                        resp.startswith(b"HTTP/1.1 403"))
        finally:
            broker.stop()
        for entry in broker.log():
            self.assertEqual(entry["decision"], "denied")
            self.assertEqual(entry["target"], "unparsed")

    def test_connection_cap(self):
        mb = _broker_mod()
        upstream = _EchoUpstream()
        try:
            broker = mb.ConnectBroker(
                allow=(("127.0.0.1", upstream.port),),
                max_connections=1)
            try:
                port = broker.start()
                first = socket.create_connection(
                    ("127.0.0.1", port), timeout=5)
                first.settimeout(5)
                try:
                    first.sendall(
                        ("CONNECT 127.0.0.1:%d HTTP/1.1\r\n"
                         "Host: x\r\n\r\n"
                         % upstream.port).encode("ascii"))
                    head = b""
                    while b"\r\n\r\n" not in head:
                        head += first.recv(4096)
                    self.assertTrue(
                        head.startswith(b"HTTP/1.1 200"))
                    resp = _proxy_request(
                        port,
                        ("CONNECT 127.0.0.1:%d HTTP/1.1\r\n"
                         "Host: x\r\n\r\n"
                         % upstream.port).encode("ascii"))
                    self.assertTrue(
                        resp.startswith(b"HTTP/1.1 403"))
                finally:
                    first.close()
            finally:
                broker.stop()
        finally:
            upstream.close()

    def test_open_tunnel_visible_in_log(self):
        mb = _broker_mod()
        upstream = _EchoUpstream()
        try:
            broker = mb.ConnectBroker(
                allow=(("127.0.0.1", upstream.port),))
            try:
                port = broker.start()
                sock = socket.create_connection(
                    ("127.0.0.1", port), timeout=5)
                sock.settimeout(5)
                try:
                    sock.sendall(
                        ("CONNECT 127.0.0.1:%d HTTP/1.1\r\n"
                         "Host: x\r\n\r\n"
                         % upstream.port).encode("ascii"))
                    head = b""
                    while b"\r\n\r\n" not in head:
                        head += sock.recv(4096)
                    self.assertTrue(
                        head.startswith(b"HTTP/1.1 200"))
                    sock.sendall(b"live-bytes")
                    self.assertEqual(sock.recv(4096),
                                     b"live-bytes")
                    # Tunnel still open: the entry must
                    # already be visible, with no close time.
                    mid = broker.log()
                    self.assertEqual(len(mid), 1)
                    self.assertEqual(mid[0]["decision"],
                                     "allowed")
                    self.assertIsNone(mid[0]["at_close"])
                    self.assertIn(str(upstream.port),
                                  mid[0]["target"])
                    result = broker.stop()
                    self.assertEqual(result["alive"], [])
                    # After stop the same entry carries the
                    # final counts and close time.
                    final = broker.log()
                    self.assertEqual(len(final), 1)
                    self.assertEqual(final[0]["decision"],
                                     "allowed")
                    self.assertGreaterEqual(
                        final[0]["bytes_up"], 10)
                    self.assertGreaterEqual(
                        final[0]["bytes_down"], 10)
                    self.assertIsNotNone(
                        final[0]["at_close"])
                    self.assertLessEqual(
                        final[0]["at_open"],
                        final[0]["at_close"])
                finally:
                    sock.close()
            finally:
                broker.stop()
        finally:
            upstream.close()

    def test_default_idle_timeout_is_300(self):
        mb = _broker_mod()
        self.assertEqual(mb._DEFAULT_IDLE_TIMEOUT_S, 300.0)
        self.assertEqual(mb.ConnectBroker()._idle_timeout_s,
                         300.0)

    def test_explicit_small_idle_timeout_honored(self):
        mb = _broker_mod()
        broker = mb.ConnectBroker(idle_timeout_s=0.2)
        try:
            self.assertEqual(broker._idle_timeout_s, 0.2)
        finally:
            broker.stop()

    def test_log_is_a_copy(self):
        mb = _broker_mod()
        broker = mb.ConnectBroker()
        try:
            port = broker.start()
            _proxy_request(
                port,
                b"CONNECT 127.0.0.1:1 HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n\r\n")
        finally:
            broker.stop()
        first = broker.log()
        first.append({"forged": True})
        first[0]["decision"] = "forged"
        second = broker.log()
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["decision"], "denied")


class SpawnBrokerCase(Worker273Mixin, unittest.TestCase):
    def setUp(self):
        self._worker_fixture()

    def _spawn_root(self):
        root_tmp = tempfile.TemporaryDirectory(
            prefix="msp296-root-")
        self.addCleanup(root_tmp.cleanup)
        root = os.path.realpath(root_tmp.name)
        os.chmod(root, 0o700)
        return root

    def _close_quiet(self, handle):
        try:
            muse_mod.msp_worker_close(handle, reason="t")
        except muse_mod.MspError:
            pass

    def test_spawn_starts_broker_and_passes_proxy_flag(self):
        root = self._spawn_root()
        child = _MspScriptedChild(script={})
        self.addCleanup(child.force_close)
        self._spawn_with(child)
        handle = muse_mod.spawn_msp_worker(
            root, profile="subscription-broker")
        self.addCleanup(self._close_quiet, handle)
        try:
            rec = muse_mod._MSP_WORKERS[handle]
            self.assertEqual(rec["profile"],
                             "subscription-broker")
            broker = rec["worker_broker"]
            port = rec["worker_broker_port"]
            self.assertTrue(1023 < port < 65536)
            cmd, env, _cwd = self.spawned[-1]
            flag = ('endpoint_transport.proxy='
                    '"http://127.0.0.1:%d"' % port)
            self.assertIn("-c", cmd)
            self.assertIn(flag, cmd)
            home = pwd.getpwuid(os.getuid()).pw_dir
            self.assertEqual(env["HOME"], home)
            # The broker is alive: a denied CONNECT gets 403.
            resp = _proxy_request(
                port,
                b"CONNECT 127.0.0.1:1 HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n\r\n")
            self.assertTrue(resp.startswith(b"HTTP/1.1 403"))
            self.assertIsNotNone(broker)
        finally:
            self._close(handle)

    def test_close_stops_broker(self):
        root = self._spawn_root()
        child = _MspScriptedChild(script={})
        self.addCleanup(child.force_close)
        self._spawn_with(child)
        handle = muse_mod.spawn_msp_worker(
            root, profile="subscription-broker")
        rec = muse_mod._MSP_WORKERS[handle]
        port = rec["worker_broker_port"]
        broker = rec["worker_broker"]
        self._close(handle)
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port),
                                     timeout=2)
        self.assertEqual(broker._stopped_for_test(), True)

    def test_spawn_failure_stops_broker(self):
        root = self._spawn_root()
        child = _MspScriptedChild(script={})
        self.addCleanup(child.force_close)
        self._spawn_with(child)
        real_wrapper = muse_mod._msp_build_wrapper

        def _boom(*args, **kwargs):
            raise muse_mod.MspError("boom", code="containment")

        brokers = []
        real_broker = _broker_mod().ConnectBroker

        class _SpyBroker(real_broker):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                brokers.append(self)

        import openmainty.muse as muse_self
        orig = muse_self._BROKER_CLASS
        muse_self._BROKER_CLASS = _SpyBroker
        try:
            muse_mod._msp_build_wrapper = _boom
            with self.assertRaises(muse_mod.MspError):
                muse_mod.spawn_msp_worker(
                    root, profile="subscription-broker")
        finally:
            muse_mod._msp_build_wrapper = real_wrapper
            muse_self._BROKER_CLASS = orig
        self.assertEqual(len(brokers), 1)
        self.assertEqual(brokers[0]._stopped_for_test(), True)

    def test_broker_log_read_only(self):
        root = self._spawn_root()
        child = _MspScriptedChild(script={})
        self.addCleanup(child.force_close)
        self._spawn_with(child)
        handle = muse_mod.spawn_msp_worker(
            root, profile="subscription-broker")
        try:
            rec = muse_mod._MSP_WORKERS[handle]
            port = rec["worker_broker_port"]
            _proxy_request(
                port,
                b"CONNECT 127.0.0.1:1 HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n\r\n")
            first = muse_mod.msp_worker_broker_log(handle)
            self.assertEqual(len(first), 1)
            first.append({"forged": True})
            second = muse_mod.msp_worker_broker_log(handle)
            self.assertEqual(len(second), 1)
        finally:
            self._close(handle)

    def test_close_returns_final_broker_log(self):
        root = self._spawn_root()
        child = _MspScriptedChild(script={})
        self.addCleanup(child.force_close)
        self._spawn_with(child)
        handle = muse_mod.spawn_msp_worker(
            root, profile="subscription-broker")
        rec = muse_mod._MSP_WORKERS[handle]
        port = rec["worker_broker_port"]
        _proxy_request(
            port,
            b"CONNECT 127.0.0.1:1 HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n\r\n")
        closed = muse_mod.msp_worker_close(
            handle, reason="t")
        self.assertIs(closed["handle_closed"], True)
        self.assertIn("broker_log", closed)
        self.assertIn("broker_stop", closed)
        self.assertEqual(len(closed["broker_log"]), 1)
        self.assertEqual(
            closed["broker_log"][0]["decision"], "denied")
        self.assertEqual(
            closed["broker_stop"]["alive"], [])

    def test_close_shape_unchanged_without_broker(self):
        root = self._spawn_root()
        child = _MspScriptedChild(script={})
        self.addCleanup(child.force_close)
        self._spawn_with(child)
        handle = muse_mod.spawn_msp_worker(
            root, profile="subscription")
        closed = muse_mod.msp_worker_close(
            handle, reason="t")
        self.assertIs(closed["handle_closed"], True)
        self.assertNotIn("broker_log", closed)
        self.assertNotIn("broker_stop", closed)
        self.assertFalse(
            [key for key in closed if "broker" in key])

    def test_broker_log_refuses_other_profiles(self):
        root = self._spawn_root()
        child = _MspScriptedChild(script={})
        self.addCleanup(child.force_close)
        self._spawn_with(child)
        handle = muse_mod.spawn_msp_worker(
            root, profile="subscription")
        try:
            with self.assertRaises(muse_mod.MspError) as cm:
                muse_mod.msp_worker_broker_log(handle)
            self.assertEqual(cm.exception.code, "bad-input")
        finally:
            self._close(handle)


class BrokerGatesCase(unittest.TestCase):
    def _rec(self, profile, flag=False, wire=False):
        return {"profile": profile,
                "worker_canary_turn_sent": flag,
                "worker_canary_wire_used": wire}

    def test_watch_armed_on_broker(self):
        rec = self._rec("subscription-broker", flag=True)
        self.assertTrue(muse_mod._canary_watch_armed(rec))
        rec = self._rec("subscription-broker", flag=False)
        self.assertFalse(muse_mod._canary_watch_armed(rec))

    def test_wire_gate_accepts_broker_once(self):
        import openmainty.muse as muse_self
        rec = self._rec("subscription-broker", flag=True,
                        wire=False)
        calls = []
        orig_record = muse_self._worker_record
        orig_owner = muse_self._worker_check_owner
        orig_exchange = muse_self._msp_exchange
        muse_self._worker_record = lambda _h: rec
        muse_self._worker_check_owner = lambda _r: None
        muse_self._msp_exchange = lambda *a: calls.append(a)
        try:
            muse_self._worker_send_request(
                "h", "turn/start", {}, 5)
            self.assertTrue(rec["worker_canary_wire_used"])
            with self.assertRaises(
                    muse_self.MspError) as cm:
                muse_self._worker_send_request(
                    "h", "turn/start", {}, 5)
            self.assertEqual(cm.exception.code,
                             "canary-already-sent")
        finally:
            muse_self._worker_record = orig_record
            muse_self._worker_check_owner = orig_owner
            muse_self._msp_exchange = orig_exchange

    def test_canary_turn_accepts_broker_profile(self):
        # The profile gate (not the wire): a broker handle
        # must pass the same profile check the canary
        # profile does. Full turn flow stays in test_294.
        self.assertIn(
            "subscription-broker",
            muse_mod._MSP_TURN_PROFILES)
        self.assertIn(
            "subscription-canary",
            muse_mod._MSP_TURN_PROFILES)


if __name__ == "__main__":
    unittest.main()
