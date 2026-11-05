"""S9 local-API hardening: Host/Origin/Content-Type gate.

Spins the real HTTP server on an ephemeral loopback port and replays
foreign requests over raw sockets (urllib cannot forge Host): evil
Origin + evil Host + text/plain, rebinding Hosts, Origin null, and
the legitimate same-origin / Origin-less flows. Temp HOME throughout.
"""
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import m2, repos, sched, serve  # noqa: E402
try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402

PROFILE = {"name": "fakerepo", "github": "example/fakerepo",
           "baseBranch": "main", "knowledge": [], "checks": [],
           "e2e": [], "release": {"notes": "CHANGELOG.md"},
           "announce": {"medium": False, "discord": False, "slack": False}}


class OriginGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_temp_home(self, str(Path(self.tmp.name) / "home"))
        os.environ.pop("META_API_KEY", None)
        repos.save(dict(PROFILE))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)

    def _stop_server(self):
        self.httpd.shutdown()
        self.thread.join(timeout=15)
        self.httpd.server_close()
        os.environ.pop("META_API_KEY", None)

    def _raw(self, method, path, headers, body=b""):
        import socket
        lines = ["%s %s HTTP/1.1" % (method, path)]
        base = {"Host": "127.0.0.1:%d" % self.port,
                "Connection": "close",
                "Content-Length": str(len(body))}
        base.update(headers)
        for key, value in base.items():
            lines.append("%s: %s" % (key, value))
        sock = socket.create_connection(("127.0.0.1", self.port),
                                        timeout=10)
        try:
            sock.sendall(("\r\n".join(lines)).encode("latin1") +
                         b"\r\n\r\n" + body)
            data = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
        finally:
            sock.close()
        head, _, rest = data.partition(b"\r\n\r\n")
        status = int(head.split(b" ", 2)[1])
        return status, head.decode("latin1"), rest

    def _json_post(self, path, obj, extra=None):
        import json
        headers = {"Content-Type": "application/json"}
        headers.update(extra or {})
        return self._raw("POST", path, headers,
                         json.dumps(obj).encode())

    def test_1_evil_probe_sched_config(self):
        before = sched.load("fakerepo")
        status, head, body = self._raw(
            "POST", "/api/sched/config",
            {"Host": "attacker.example:8901",
             "Origin": "https://evil.example",
             "Content-Type": "text/plain"}, b"{}")
        self.assertEqual(status, 403)
        self.assertNotIn("repo is required", body.decode())
        after = sched.load("fakerepo")
        self.assertEqual(before.get("interval_s"), after.get("interval_s"))
        self.assertEqual(before.get("execute_enabled"),
                         after.get("execute_enabled"))

    def test_2_evil_probe_m2_policy(self):
        before = m2.get_policy("fakerepo")
        status, _, _ = self._raw(
            "POST", "/api/m2/policy",
            {"Host": "attacker.example:8901",
             "Origin": "https://evil.example",
             "Content-Type": "text/plain"}, b"{}")
        self.assertEqual(status, 403)
        # The refused probe must not flip any toggle.
        self.assertEqual(m2.get_policy("fakerepo"), before)

    def test_3_foreign_host_get(self):
        status, _, _ = self._raw("GET", "/api/app",
                                 {"Host": "attacker.example:8901"})
        self.assertEqual(status, 403)
        status, _, _ = self._raw(
            "GET", "/api/app", {"Host": "127.0.0.1:9"})
        self.assertEqual(status, 403)
        status, _, _ = self._raw("GET", "/api/app", {})
        self.assertEqual(status, 200)

    def test_4_origin_null_refused(self):
        status, _, _ = self._raw(
            "POST", "/api/m2/policy",
            {"Origin": "null", "Content-Type": "application/json"},
            b'{"repo":"fakerepo"}')
        self.assertEqual(status, 403)

    def test_5_text_plain_with_good_origin_refused(self):
        status, _, _ = self._raw(
            "POST", "/api/m2/policy",
            {"Origin": "http://127.0.0.1:%d" % self.port,
             "Content-Type": "text/plain"}, b"{}")
        self.assertEqual(status, 403)

    def test_6_legit_posts_pass(self):
        status, _, body = self._json_post(
            "/api/m2/policy", {"repo": "fakerepo"},
            {"Origin": "http://127.0.0.1:%d" % self.port})
        self.assertEqual(status, 200)
        self.assertIn(b"fakerepo", body)
        status, _, _ = self._json_post("/api/m2/policy",
                                       {"repo": "fakerepo"})
        self.assertEqual(status, 200)

    def test_7_no_cors_headers(self):
        for (method, path, headers, body) in [
                ("GET", "/api/app", {}, b""),
                ("POST", "/api/m2/policy",
                 {"Content-Type": "application/json"},
                 b'{"repo":"fakerepo"}')]:
            _, head, _ = self._raw(method, path, headers, body)
            self.assertNotIn("access-control", head.lower())


if __name__ == "__main__":
    unittest.main()
