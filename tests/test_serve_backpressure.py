"""G9b: one wedged checkout never makes a healthy repo look broken.

repos.detect_fastcheck reads <checkout>/package.json directly with
read_text (no is_file guard), so a FIFO there blocks open()
deterministically. Every test uses a fresh mktemp HOME and
OPENMAINTY_HOME, starts its own server on an ephemeral port, and
never touches the live app, real checkouts, or the network.

Covered:
- per-root fail-fast: 5 parked reads on root A, detect + watch on
  healthy root B succeed within 1 s, with no message naming B;
- overview: first pass blocks once on the wedged root (<= 6 s),
  the next pass fail-fasts (<= 1 s); flags A true / B false;
- stale work never runs: a call refused while wedged never executes,
  even after recovery; only a new call runs;
- recovery without restart, by writer and by replacing the file;
- reserve paths, 503 table, thread/fd bounds (storm);
- band names an unreadable repo missing from summary rows (driver);
- onboarding caps repaint before detect returns (driver).
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import serve  # noqa: E402


def _fetch(port, path, timeout=10):
    """GET returning (status, headers dict, body bytes); no raise."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, dict(res.headers), res.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()


def _post(port, path, payload, timeout=10):
    """POST JSON returning (status, headers dict, body bytes)."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, dict(res.headers), res.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()


class BackpressureCase(unittest.TestCase):
    DETECT_A = {"repo": "repoA"}
    DETECT_B = {"repo": "repoB"}

    def setUp(self):
        self._old = (os.environ.get("HOME"), os.environ.get("OPENMAINTY_HOME"))
        self._home = tempfile.mkdtemp(prefix="om236-bp-")
        os.environ["HOME"] = self._home
        os.environ["OPENMAINTY_HOME"] = self._home
        self._old_timeout = serve._CHECKOUT_TIMEOUT_S
        serve._CHECKOUT_TIMEOUT_S = 1.0
        self.addCleanup(self._restore)
        self._fifos = []
        # Root A: wedged (package.json is a FIFO). Root B: healthy.
        self._coA = os.path.join(self._home, "coA")
        self._coB = os.path.join(self._home, "coB")
        os.makedirs(self._coA, exist_ok=True)
        os.makedirs(self._coB, exist_ok=True)
        fifo = os.path.join(self._coA, "package.json")
        os.mkfifo(fifo)
        self._fifos.append(fifo)
        with open(os.path.join(self._coB, "package.json"), "w") as fh:
            json.dump({"name": "repoB"}, fh)
        with open(os.path.join(self._home, "pc.json"), "w") as fh:
            json.dump({"checkouts": {"repoA": self._coA,
                                     "repoB": self._coB}}, fh)
        os.makedirs(os.path.join(self._home, "repos"), exist_ok=True)
        for name in ("repoA", "repoB"):
            with open(os.path.join(self._home, "repos", name + ".json"),
                      "w") as fh:
                json.dump({"name": name,
                           "github": "example-org/fake"}, fh)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        self._port = srv.server_address[1]
        runner = threading.Thread(target=srv.serve_forever, daemon=True)
        runner.start()
        self._srv = srv
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)

    def _restore(self):
        serve._CHECKOUT_TIMEOUT_S = self._old_timeout
        # Release threads parked in blocking open(): one O_RDWR pass
        # frees exactly one pending O_RDONLY opener on macOS, so
        # repeat. O_RDWR never blocks; extra passes only buffer bytes
        # nobody reads (each test uses a fresh home/fifo).
        import stat as _stat
        for path in self._fifos:
            try:
                if not _stat.S_ISFIFO(os.stat(path).st_mode):
                    continue
            except OSError:
                continue

            def _w(p=path):
                for _ in range(6):
                    try:
                        fd = os.open(p, os.O_RDWR | os.O_NONBLOCK)
                    except OSError:
                        return
                    try:
                        try:
                            os.write(fd, b"{}\n")
                        except OSError:
                            pass
                    finally:
                        os.close(fd)
                    time.sleep(0.05)
            t = threading.Thread(target=_w, daemon=True)
            t.start()
            t.join(timeout=15)
        old_home, old_om = self._old
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home
        if old_om is None:
            os.environ.pop("OPENMAINTY_HOME", None)
        else:
            os.environ["OPENMAINTY_HOME"] = old_om
        shutil.rmtree(self._home, ignore_errors=True)

    def _park_on_A(self, n):
        """Park n checkout reads on root A; return their threads."""
        out = []

        def _hit():
            try:
                serve.checkout_io(open, os.path.join(self._coA,
                                                     "package.json"),
                                  path=self._coA)
            except serve.ApiError:
                pass
        for _ in range(n):
            t = threading.Thread(target=_hit, daemon=True)
            t.start()
            out.append(t)
        # Every parked read must have registered before we proceed:
        # each takes up to the 1 s budget to time out.
        time.sleep(1.5)
        return out

    # -- unit: the helper itself --------------------------------------

    def test_import_starts_no_threads(self):
        """A8: importing openmainty.serve starts zero threads.

        Daemon workers exist only for the duration of a checkout_io
        call; nothing parks in queue.get at import time, so helpers
        that join every live thread (e.g. discussions _wait_job)
        cannot hang on product threads.
        """
        import subprocess
        src = str(Path(__file__).resolve().parent.parent / "src")
        code = ("import threading, sys; sys.path.insert(0, %r); "
                "a = threading.active_count(); "
                "import openmainty.serve; "
                "b = threading.active_count(); print(a, b)" % src)
        proc = subprocess.run(
            [sys.executable, "-B", "-c", code],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        before, after = (int(x) for x in proc.stdout.split())
        self.assertEqual(before, after)

    def test_checkout_io_passes_value_through(self):
        self.assertEqual(serve.checkout_io(lambda a, b: a + b, 2, 3,
                                          path="x"), 5)

    def test_checkout_io_timeout_is_fail_closed_korean(self):
        never = threading.Event()
        t0 = time.monotonic()
        with self.assertRaises(serve.ApiError) as cm:
            serve.checkout_io(never.wait, path="/ext/fakerepo")
        self.assertEqual(cm.exception.status, 502)
        self.assertIn("응답이 없습니다", cm.exception.message)
        self.assertIn("/ext/fakerepo", cm.exception.message)
        self.assertLess(time.monotonic() - t0, 5.0)

    def test_checkout_io_reraises_other_errors(self):
        def _boom():
            raise ValueError("nope")
        with self.assertRaises(ValueError):
            serve.checkout_io(_boom, path="x")

    def test_checkout_io_fifo_open_blocks_then_fails_closed(self):
        fifo = os.path.join(self._home, "blocker")
        os.mkfifo(fifo)
        self._fifos.append(fifo)
        t0 = time.monotonic()
        with self.assertRaises(serve.ApiError) as cm:
            serve.checkout_io(open, fifo, path=fifo)
        self.assertEqual(cm.exception.status, 502)
        self.assertLess(time.monotonic() - t0, 5.0)

    def test_fail_fast_is_instant_on_same_identity(self):
        parked = self._park_on_A(1)
        try:
            t0 = time.monotonic()
            with self.assertRaises(serve.ApiError) as cm:
                serve.checkout_io(open, os.path.join(self._coA,
                                                     "package.json"),
                                  path=self._coA)
            self.assertLess(time.monotonic() - t0, 0.5)
            self.assertEqual(cm.exception.status, 502)
            self.assertIn(self._coA, cm.exception.message)
        finally:
            for t in parked:
                t.join(timeout=15)

    def test_replaced_file_probes_instead_of_fail_fast(self):
        parked = self._park_on_A(1)
        try:
            fifo = os.path.join(self._coA, "package.json")
            os.remove(fifo)
            with open(fifo, "w") as fh:
                json.dump({"name": "repoA"}, fh)
            # Same path, new identity: must probe and succeed, not
            # fail fast on the dead FIFO's record.
            out = serve.checkout_io(
                lambda: json.loads(open(fifo).read()), path=self._coA)
            self.assertEqual(out, {"name": "repoA"})
        finally:
            for t in parked:
                t.join(timeout=15)

    # -- HTTP: healthy root answers while A is wedged ------------------

    def test_healthy_roots_answer_while_A_parked(self):
        parked = self._park_on_A(5)
        try:
            t0 = time.monotonic()
            st, _hdrs, body = _post(self._port, "/api/repo/detect",
                                    self.DETECT_B, timeout=10)
            dt = time.monotonic() - t0
            self.assertEqual(st, 200)
            self.assertLess(dt, 1.0)
            t0 = time.monotonic()
            st, _hdrs, body = _post(self._port, "/api/watch",
                                    {"repo": "repoB"}, timeout=15)
            dt = time.monotonic() - t0
            self.assertEqual(st, 200)
            self.assertLess(dt, 5.0)
            # A fail-fasts too instead of parking a sixth thread.
            t0 = time.monotonic()
            st, _hdrs, body = _post(self._port, "/api/repo/detect",
                                    self.DETECT_A, timeout=10)
            dt = time.monotonic() - t0
            self.assertEqual(st, 502)
            self.assertLess(dt, 1.0)
            err = json.loads(body)["error"]
            self.assertIn(self._coA, err)
            self.assertNotIn(self._coB, err)
        finally:
            for t in parked:
                t.join(timeout=15)

    def test_app_and_page_answer_while_detect_blocked(self):
        latched = []
        errs = []

        def _hit():
            try:
                latched.append(_post(self._port, "/api/repo/detect",
                                     self.DETECT_A, timeout=15))
            except Exception as exc:  # noqa: BLE001
                errs.append(exc)

        workers = [threading.Thread(target=_hit, daemon=True)
                   for _ in range(4)]
        for t in workers:
            t.start()
        time.sleep(0.3)
        t0 = time.monotonic()
        st, _hdrs, body = _fetch(self._port, "/api/app", timeout=10)
        dt = time.monotonic() - t0
        self.assertEqual(st, 200)
        app = json.loads(body)
        for key in ("in_flight", "handler_cap", "open_fds", "degraded"):
            self.assertIn(key, app)
        self.assertEqual(app["handler_cap"], serve._HANDLER_CAP)
        self.assertLess(dt, 1.0)
        t0 = time.monotonic()
        st, _hdrs, _body = _fetch(self._port, "/", timeout=10)
        self.assertEqual(st, 200)
        self.assertLess(time.monotonic() - t0, 1.0)
        for t in workers:
            t.join(timeout=15)
        self.assertEqual(errs, [])
        self.assertEqual(len(latched), 4)
        for st, _hdrs, body in latched:
            # First parks full-budget; the rest fail fast on the
            # same identity. All fail closed in Korean.
            self.assertEqual(st, 502)
            self.assertIn("응답이 없습니다",
                          json.loads(body)["error"])

    # -- HTTP: full handler table fails fast with 503 ------------------

    def test_full_table_returns_503_with_retry_after(self):
        held = []
        try:
            for _ in range(serve._HANDLER_CAP):
                self.assertTrue(
                    serve._HANDLER_SEM.acquire(blocking=False))
                held.append(True)
            t0 = time.monotonic()
            st, hdrs, body = _fetch(self._port, "/api/overview",
                                    timeout=10)
            dt = time.monotonic() - t0
            self.assertEqual(st, 503)
            self.assertEqual(hdrs.get("Retry-After"), "5")
            self.assertIn("바쁩니다", json.loads(body)["error"])
            self.assertLess(dt, 2.0)
            st, _hdrs, _body = _fetch(self._port, "/api/app",
                                      timeout=10)
            self.assertEqual(st, 200)
            st, _hdrs, _body = _fetch(self._port, "/", timeout=10)
            self.assertEqual(st, 200)
        finally:
            for _ in held:
                serve._HANDLER_SEM.release()

    # -- HTTP: blocked, resume, and stale-never-runs -------------------

    def test_blocked_detect_then_resume_without_restart(self):
        st, _hdrs, body = _post(self._port, "/api/repo/detect",
                                self.DETECT_A, timeout=15)
        self.assertEqual(st, 502)
        self.assertIn("응답이 없습니다", json.loads(body)["error"])
        fifo = os.path.join(self._coA, "package.json")
        os.remove(fifo)
        with open(fifo, "w") as fh:
            json.dump({"name": "repoA",
                       "scripts": {"test": "node t.js"}}, fh)
        st, _hdrs, body = _post(self._port, "/api/repo/detect",
                                self.DETECT_A, timeout=15)
        self.assertEqual(st, 200)
        self.assertIn("detection", json.loads(body))

    def test_stale_call_never_runs_after_recovery(self):
        calls = []

        def _fn():
            calls.append(1)
            return "v"
        fifo = os.path.join(self._coA, "package.json")
        parked = self._park_on_A(1)
        try:
            # Refused while wedged: never spawned, never executed.
            with self.assertRaises(serve.ApiError):
                serve.checkout_io(_fn, path=self._coA)
            self.assertEqual(calls, [])
            # Release the parked worker by pairing with its blocked
            # open (O_RDWR never blocks); it returns and clears the
            # root, but the refused call still never runs later.
            fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
            os.close(fd)
        finally:
            for t in parked:
                t.join(timeout=15)
        for t in parked:
            self.assertFalse(t.is_alive())
        # The joined threads are the _hit WRAPPERS, not the per-read
        # daemon the O_RDWR open released: its epilogue (which clears
        # the stuck registry) may not have run yet, and the product
        # stays fail-closed for those few ms. Poll with a deadline
        # until a probe call stops failing fast. The probe runs a
        # no-op, so the calls log below still counts only real runs.
        deadline = time.monotonic() + 5.0
        while True:
            try:
                serve.checkout_io(lambda: None, path=self._coA)
                break
            except serve.ApiError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)
        # After the parked read returns, a NEW call runs exactly once.
        self.assertEqual(serve.checkout_io(_fn, path=self._coA), "v")
        self.assertEqual(calls, [1])

    # -- HTTP: overview latency and flags with A wedged ---------------

    def test_overview_first_slow_then_fast_with_correct_flags(self):
        t0 = time.monotonic()
        st, _hdrs, body = _fetch(self._port, "/api/overview",
                                 timeout=30)
        first = time.monotonic() - t0
        self.assertEqual(st, 200)
        rows = {r["name"]: r for r in json.loads(body)["repos"]}
        self.assertTrue(rows["repoA"]["checkout_unreadable"])
        self.assertIn("응답이 없습니다", rows["repoA"]["drift"])
        self.assertFalse(rows["repoB"]["checkout_unreadable"])
        self.assertLess(first, 6.0)
        t0 = time.monotonic()
        st, _hdrs, body = _fetch(self._port, "/api/overview",
                                 timeout=30)
        second = time.monotonic() - t0
        self.assertEqual(st, 200)
        rows = {r["name"]: r for r in json.loads(body)["repos"]}
        self.assertTrue(rows["repoA"]["checkout_unreadable"])
        self.assertFalse(rows["repoB"]["checkout_unreadable"])
        self.assertLess(second, 1.0)

    # -- bounds: threads and fds through a request storm ---------------

    def test_storm_stays_bounded(self):
        base_threads = threading.active_count()
        base_fds = len(os.listdir("/dev/fd"))
        results = []
        errs = []
        gate = threading.Barrier(40)

        def _hit():
            try:
                gate.wait(timeout=15)
                results.append(_post(self._port, "/api/repo/detect",
                                     self.DETECT_A, timeout=25))
            except Exception as exc:  # noqa: BLE001
                errs.append(exc)

        workers = [threading.Thread(target=_hit, daemon=True)
                   for _ in range(40)]
        for t in workers:
            t.start()
        for t in workers:
            t.join(timeout=40)
        self.assertEqual(errs, [])
        self.assertEqual(len(results), 40)
        seen = {}
        for st, hdrs, body in results:
            seen[st] = seen.get(st, 0) + 1
            if st == 502:
                self.assertIn("응답이 없습니다",
                              json.loads(body)["error"])
            elif st == 503:
                self.assertEqual(hdrs.get("Retry-After"), "5")
            else:
                self.fail(f"unexpected storm status {st}")
        self.assertIn(502, seen)
        self.assertIn(503, seen)
        # Every handler finished; only per-read daemon threads stay
        # parked (released by cleanup).
        deadline = time.monotonic() + 5.0
        while (threading.active_count() > base_threads + 40
               and time.monotonic() < deadline):
            time.sleep(0.05)
        self.assertLessEqual(threading.active_count(),
                             base_threads + 40)
        self.assertLess(len(os.listdir("/dev/fd")), base_fds + 80)


def _dashboard_source():
    return (Path(__file__).resolve().parent.parent
            / "src" / "openmainty" / "dashboard.html").read_text(
                encoding="utf-8")


def _one_line(source, prefix):
    """One top-level line starting with prefix; exactly one match."""
    found = [line for line in source.splitlines()
             if line.startswith(prefix)]
    if len(found) != 1:
        raise AssertionError(
            "expected exactly one %r line, got %d" % (prefix,
                                                      len(found)))
    return found[0]


def _balanced_js(source, prefix):
    """One top-level function/const starting with prefix.

    dashboard.html keeps top-level function bodies indented, so the
    block ends at the first later line that is exactly "}". Nested
    template literals would defeat a naive brace counter, and this
    needs no counting at all.
    """
    lines = source.splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines)
                 if line.startswith(prefix))
    end = next(i for i in range(start + 1, len(lines))
               if lines[i] == "}\n")
    return "".join(lines[start:end + 1])


def _band_js():
    """Live band renderers extracted verbatim from dashboard.html."""
    src = _dashboard_source()
    parts = [
        _one_line(src, "function esc("),
        _balanced_js(src, "function ago("),
        _one_line(src, "const M1_VERDICT_CLS ="),
        _one_line(src, "const M1_VERDICT_KO ="),
        _one_line(src, "const LOGIN_NEEDED_KO ="),
        _one_line(src, "const APIKEY_SET_KO ="),
        _one_line(src, "const BAND_STALE_TOL_MS ="),
        _balanced_js(src, "function bandAskId("),
        _balanced_js(src, "function glanceBandVerdict("),
        _balanced_js(src, "function glanceBandSentence("),
        _balanced_js(src, "function glanceBandStateKo("),
        _balanced_js(src, "function glanceBandHtml("),
    ]
    return "\n".join(parts)


class BandAndOnboardingTests(unittest.TestCase):
    """A7e: glance honesty and onboarding repaint, no server needed."""

    def test_band_names_unreadable_missing_from_summary(self):
        """The band names an unreadable repo even when the summary
        rows do not list it (newly registered), and never marks a
        healthy repo 응답 없음. Runs the shipped JS in node."""
        import subprocess
        scenario = (
            "const summary = %s;\n"
            "const desk = %s;\n"
            "const overview = %s;\n"
            "const band = glanceBandVerdict(summary, desk, overview);\n"
            "const html = glanceBandHtml(band);\n"
            "console.log(JSON.stringify({state: band.state,\n"
            "  unreadable: band.unreadableRepos, rows: band.repos,\n"
            "  html}));\n" % (
                json.dumps({
                    "at": "2026-10-25T00:00:00+00:00",
                    "repos": [{"name": "repoB", "verdict": "normal",
                               "blocked": [], "need_you": []}]}),
                json.dumps({"supported": []}),
                json.dumps({"repos": [
                    {"name": "repoA",
                     "checkout_unreadable": True,
                     "sched_enabled": False},
                    {"name": "repoB",
                     "checkout_unreadable": False,
                     "sched_enabled": False}]})))
        with tempfile.NamedTemporaryFile("w", suffix=".js",
                                         delete=False,
                                         encoding="utf-8") as fh:
            fh.write(_band_js() + "\n" + scenario)
            name = fh.name
        try:
            proc = subprocess.run(["node", name], capture_output=True,
                                  text=True, timeout=120)
        finally:
            try:
                os.unlink(name)
            except OSError:
                pass
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        got = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(got["state"], "problem")
        self.assertEqual(got["unreadable"], ["repoA"])
        self.assertEqual(got["rows"],
                         [{"name": "repoB", "state": "normal"}])
        html = got["html"]
        # The wedged repo is named even though no summary row lists it.
        self.assertIn("저장소 응답 없음: <b>repoA</b>", html)
        self.assertIn("저장소 응답 없음: 1곳", html)
        # The healthy row carries no 응답 없음 mark.
        seg = html.split("<b>repoB</b>", 1)[1].split("</div>", 1)[0]
        self.assertNotIn("응답 없음", seg)
        # 390 px: no fixed wide box; band buttons keep a 44 px target.
        self.assertNotRegex(html, r"width:\s*\d{3,}px")
        css = _dashboard_source()
        self.assertIn("[data-band=\"1\"] .btn", css)
        self.assertIn("min-height: 44px", css)

    def test_onboarding_caps_repaint_before_detect(self):
        """A6: caps paint again as soon as caps arrive, while detect
        is still pending — no 5 s false '읽는 중…'. Static order
        check on the shipped onboarding flow."""
        src = _dashboard_source()
        i_caps = src.index('caps = await jget("/api/onboard/caps")')
        i_detect = src.index('jpost("/api/repo/detect"', i_caps)
        self.assertIn("paint();", src[i_caps:i_detect])


if __name__ == "__main__":
    unittest.main()
