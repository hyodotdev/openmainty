"""Slice 295b: canary read lines are DNS-only (gate 2).

Slice 295 appended a DNS line plus a TLS-roots group
(E35a4 V2: curl needed the trust-roots file; E35 ran with
the group). The TLS group was always marked UNPROVEN for
the worker, and the cost-policy secret scanner flags its
path in product source. 295b removes the group so E36 can
test whether the worker needs it at all: the tuple is now
ONLY the E35a5 DNS line. If E36 shows the worker needs
trust roots, a narrow reviewed scanner exception for the
exact public CA path is a later Root decision — never a
reword to slip past the scanner.

openssl.cnf stays probe-only, as do keychain paths,
securityd changes, and any network line change. The plain
subscription profile stays byte-identical and
network-denied.

Offline only: pure text building, no spawn, no binary.
Isolated temp homes only; never the real home. No deletes:
TemporaryDirectory cleanup only.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from openmainty import ncontain  # noqa: E402
from test_276_subscription_profile import (  # noqa: E402
    GOLDEN_SUBSCRIPTION_TEXT)

FAKE_ROOT = "/private/tmp/fake295/root"
FAKE_HOME = "/Users/fake295"
FAKE_EXE = "/Users/fake295/.local/bin/muse-bin-test"

READ_LINES = (
    '(allow file-read-metadata (literal "/var"))',
)

TLS_GROUP_LINES = (
    '(allow file-read* '
    '(literal "/private/etc/ssl/cert.pem"))',
    '(allow file-read-metadata (literal "/etc"))',
    '(allow file-read-metadata (literal "/private/etc"))',
    '(allow file-read-metadata '
    '(literal "/private/etc/ssl"))',
)

NET_LINES = (
    '(allow network-outbound (remote tcp "*:443"))',
    '(allow network-outbound '
    '(literal "/private/var/run/mDNSResponder"))',
)

FORBIDDEN_PROBES = (
    '(allow file-read* (literal "/Users/x/Library/Keychains'
    '/login.keychain-db"))',
    "(allow mach-lookup (global-name \"com.apple.securityd\"))",
    '(allow file-read* (literal "/Users/x/.muse/x"))',
    '(allow file-read* (literal '
    '"/private/etc/ssl/openssl.cnf"))',
)


def _sub_text():
    return ncontain.build_subscription_worker_policy_text(
        FAKE_ROOT, FAKE_EXE, FAKE_HOME)


def _canary_text():
    return ncontain.build_subscription_canary_policy_text(
        FAKE_ROOT, FAKE_EXE, FAKE_HOME)


class ReadLinesCase(unittest.TestCase):
    def test_tuple_is_exact(self):
        self.assertEqual(
            ncontain.SUBSCRIPTION_CANARY_READ_LINES,
            READ_LINES)

    def test_tuple_carries_no_forbidden_path(self):
        blob = "\n".join(
            ncontain.SUBSCRIPTION_CANARY_READ_LINES)
        for probe in ("Keychains", "securityd", ".muse",
                      "openssl.cnf", "cert.pem",
                      "/private/etc/ssl"):
            self.assertNotIn(probe, blob)

    def test_tls_group_absent_from_canary_text(self):
        got = _canary_text()
        for line in TLS_GROUP_LINES:
            with self.subTest(line=line):
                self.assertNotIn(line, got)
        self.assertNotIn("cert.pem", got)

    def test_canary_shape_and_order(self):
        sub = _sub_text()
        got = _canary_text()
        want = (sub.splitlines() + list(NET_LINES)
                + list(READ_LINES))
        self.assertEqual(got.splitlines(), want)

    def test_each_read_line_once_and_absent_from_sub(self):
        sub = _sub_text()
        got = _canary_text()
        for line in READ_LINES:
            with self.subTest(line=line):
                self.assertEqual(got.count(line), 1)
                self.assertNotIn(line, sub)

    def test_proof_refuses_extra_line(self):
        sub = _sub_text()
        got = _canary_text()
        with self.assertRaises(ncontain.ContainmentError):
            ncontain._prove_canary_plus(
                got + "(allow network*)\n", sub)

    def test_proof_refuses_missing_line(self):
        sub = _sub_text()
        got = _canary_text()
        dropped = got.replace(READ_LINES[0] + "\n", "", 1)
        self.assertNotEqual(dropped, got)
        with self.assertRaises(ncontain.ContainmentError):
            ncontain._prove_canary_plus(dropped, sub)

    def test_proof_refuses_reordered_lines(self):
        sub = _sub_text()
        lines = _canary_text().splitlines()
        head, tail = lines[:-3], lines[-3:]
        # Swap the last network line with the first read
        # line; the set is identical, the order is not.
        tail[1], tail[2] = tail[2], tail[1]
        with self.assertRaises(ncontain.ContainmentError):
            ncontain._prove_canary_plus(
                "\n".join(head + tail) + "\n", sub)

    def test_proof_refuses_forbidden_lines(self):
        sub = _sub_text()
        got = _canary_text()
        for probe in FORBIDDEN_PROBES:
            with self.subTest(probe=probe):
                with self.assertRaises(
                        ncontain.ContainmentError):
                    ncontain._prove_canary_plus(
                        got + probe + "\n", sub)


class SubscriptionUnchangedCase(unittest.TestCase):
    def test_subscription_golden_unchanged(self):
        got = ncontain.build_subscription_worker_policy_text(
            "/private/tmp/fake276/root",
            "/Users/fake276/.local/bin/muse-bin-test",
            "/Users/fake276")
        self.assertEqual(got, GOLDEN_SUBSCRIPTION_TEXT)
        self.assertNotIn("network-outbound", got)
        for line in READ_LINES:
            self.assertNotIn(line, got)


class ExceptionsCase(unittest.TestCase):
    def test_read_entries_listed_with_reasons(self):
        entries = ncontain.subscription_canary_exceptions(
            FAKE_HOME)
        by_path = {entry["path"]: entry
                   for entry in entries}
        for line in READ_LINES:
            with self.subTest(line=line):
                entry = by_path[line]
                self.assertEqual(entry["kind"], "read")
                self.assertTrue(entry["reason"])
                self.assertIn("E35", entry["reason"])
        for line in NET_LINES:
            self.assertEqual(by_path[line]["kind"],
                             "network")

    def test_exactly_one_read_entry(self):
        entries = ncontain.subscription_canary_exceptions(
            FAKE_HOME)
        reads = [entry for entry in entries
                 if entry["kind"] == "read"]
        self.assertEqual(len(reads), 1)
        self.assertEqual(reads[0]["path"], READ_LINES[0])


if __name__ == "__main__":
    unittest.main()
