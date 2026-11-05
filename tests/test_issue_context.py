"""Issue-context backend: strict validation, immutable cache, no run writes.

Offline: gh transport is canned at the issue_context._run_gh boundary;
util.run_cmd is denied so nothing can reach the network. Fresh fixture
HOME per test; old run records must survive byte-identical.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import issue_context as ic


def make_home(test):
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    prev = os.environ.get("OPENMAINTY_HOME")
    os.environ["OPENMAINTY_HOME"] = str(Path(tmp.name) / "home")
    test.addCleanup(lambda: os.environ.pop("OPENMAINTY_HOME", None)
                    if prev is None else os.environ.__setitem__(
                        "OPENMAINTY_HOME", prev))

    def _no_network(*args, **kwargs):
        raise AssertionError("unexpected network call in offline test")

    net = mock.patch.object(ic._util, "run_cmd", side_effect=_no_network)
    net.start()
    test.addCleanup(net.stop)
    return Path(tmp.name)


def fake_profile():
    from openmainty import repos
    repos.save({"name": "r", "github": "o/r", "baseBranch": "main",
                "knowledge": [], "checks": [], "e2e": [],
                "release": {}, "announce": {}})


def fake_run(run_id="run-x", repo="r", issue="856"):
    from openmainty import store
    state = {"schema": 1, "id": run_id, "repo": repo, "issue": issue,
             "title": "Web demo is not working", "trigger": None,
             "why": None}
    store.save(state)
    return state


def raw_issue(number=856, **kw):
    raw = {"number": number, "title": "Web demo is not working",
           "body": "The demo shows a blank page.",
           "state": "OPEN",
           "url": "https://github.com/o/r/issues/856"}
    raw.update(kw)
    return raw


class TestValidation(unittest.TestCase):
    def test_valid_open_record(self):
        obs = ic.validate_observation(raw_issue(), "o/r", 856)
        self.assertEqual(obs["number"], 856)
        self.assertEqual(obs["state"], "OPEN")
        self.assertFalse(obs["historical_trigger"])
        self.assertEqual(len(obs["content_fp"]), 64)

    def test_closed_is_valid_data(self):
        obs = ic.validate_observation(raw_issue(state="closed"), "o/r", 856)
        self.assertEqual(obs["state"], "CLOSED")

    def test_empty_body_is_explicit_empty(self):
        obs = ic.validate_observation(raw_issue(body=""), "o/r", 856)
        self.assertEqual(obs["body"], "")

    def test_missing_body_key_is_unreadable(self):
        raw = raw_issue()
        del raw["body"]
        with self.assertRaises(ic.IssueContextError):
            ic.validate_observation(raw, "o/r", 856)

    def test_non_dict_envelope_rejected(self):
        with self.assertRaises(ic.IssueContextError):
            ic.validate_observation([1, 2], "o/r", 856)

    def test_bool_number_rejected(self):
        with self.assertRaises(ic.IssueContextError):
            ic.validate_observation(raw_issue(number=True), "o/r", 856)

    def test_number_mismatch_rejected(self):
        with self.assertRaises(ic.IssueContextError):
            ic.validate_observation(raw_issue(number=857), "o/r", 856)

    def test_foreign_url_rejected(self):
        raw = raw_issue(url="https://github.com/evil/x/issues/856")
        with self.assertRaises(ic.IssueContextError):
            ic.validate_observation(raw, "o/r", 856)

    def test_fetched_at_excluded_from_fingerprint(self):
        a = ic.validate_observation(raw_issue(), "o/r", 856)
        b = ic.validate_observation(raw_issue(), "o/r", 856)
        self.assertEqual(a["content_fp"], b["content_fp"])

    def test_body_change_changes_fingerprint(self):
        a = ic.validate_observation(raw_issue(), "o/r", 856)
        b = ic.validate_observation(raw_issue(body="changed"), "o/r", 856)
        self.assertNotEqual(a["content_fp"], b["content_fp"])

    def test_safe_number_rejects_bool_and_text(self):
        self.assertEqual(ic.safe_issue_number("856"), 856)
        for bad in (True, False, "abc", "12x", "", None, "dep-1", 0, -3):
            with self.assertRaises(ic.IssueContextError):
                ic.safe_issue_number(bad)


class TestResolve(unittest.TestCase):
    def setUp(self):
        make_home(self)
        fake_profile()

    def test_unknown_repo_404(self):
        with self.assertRaises(ic.IssueContextError) as cm:
            ic.resolve_run_context("nope", "run-x")
        self.assertEqual(cm.exception.http_status, 404)

    def test_unknown_run_404(self):
        fake_run()
        with self.assertRaises(ic.IssueContextError) as cm:
            ic.resolve_run_context("r", "run-missing")
        self.assertEqual(cm.exception.http_status, 404)

    def test_repo_mismatch_400(self):
        fake_run(run_id="run-x", repo="r")
        with self.assertRaises(ic.IssueContextError) as cm:
            ic.resolve_run_context("other", "run-x")
        # 'other' profile is unknown -> 404 is also honest; accept either
        self.assertIn(cm.exception.http_status, (400, 404))

    def test_repo_mismatch_same_profile_400(self):
        from openmainty import repos
        repos.save({"name": "s", "github": "o/s", "baseBranch": "main",
                    "knowledge": [], "checks": [], "e2e": [],
                    "release": {}, "announce": {}})
        fake_run(run_id="run-x", repo="r")
        with self.assertRaises(ic.IssueContextError) as cm:
            ic.resolve_run_context("s", "run-x")
        self.assertEqual(cm.exception.http_status, 400)

    def test_non_numeric_issue_not_applicable(self):
        fake_run(issue="dep-foo")
        with self.assertRaises(ic.IssueContextError) as cm:
            ic.resolve_run_context("r", "run-x")
        self.assertEqual(cm.exception.code, "not-applicable")

    def test_bool_issue_rejected(self):
        from openmainty import store
        store.save({"schema": 1, "id": "run-b", "repo": "r", "issue": True})
        with self.assertRaises(ic.IssueContextError) as cm:
            ic.resolve_run_context("r", "run-b")
        self.assertEqual(cm.exception.http_status, 400)

    def test_ok_binding(self):
        fake_run()
        ctx = ic.resolve_run_context("r", "run-x")
        self.assertEqual(ctx, {"slug": "o/r", "number": 856,
                               "run_id": "run-x", "repo": "r"})


def canned(raw, **kw):
    return json.dumps(raw)


class TestCacheAndFetch(unittest.TestCase):
    def setUp(self):
        make_home(self)
        fake_profile()
        fake_run()

    def run_bytes(self):
        from openmainty import store
        return store.run_file("run-x").read_bytes()

    def test_get_never_fetches(self):
        with mock.patch.object(ic, "_run_gh",
                               side_effect=AssertionError("GET fetched")):
            out = ic.read_cached("r", "run-x")
        self.assertTrue(out["applicable"])
        self.assertFalse(out["cached"])
        self.assertIsNone(out["observation"])

    def test_fetch_stores_immutable_observation(self):
        before = self.run_bytes()
        with mock.patch.object(ic, "_run_gh",
                               return_value=canned(raw_issue())):
            res = ic.fetch_and_store("r", "run-x")
        self.assertTrue(res["changed"])
        self.assertIsNone(res["previous_latest"])
        obs = res["observation"]
        self.assertEqual(obs["number"], 856)
        # fetch time lives ONLY in last-attempt metadata, never in the
        # immutable observation, so identical content reuses the bytes
        self.assertNotIn("fetched_at", obs)
        self.assertIn("gh issue view o/r#856", obs["provenance"])
        self.assertFalse(obs["historical_trigger"])
        # old run record byte-identical
        self.assertEqual(self.run_bytes(), before)
        # cached read returns it without network
        with mock.patch.object(ic, "_run_gh",
                               side_effect=AssertionError("GET fetched")):
            out = ic.read_cached("r", "run-x")
        self.assertTrue(out["cached"])
        self.assertEqual(out["observation"]["content_fp"],
                         obs["content_fp"])

    def test_identical_refetch_not_changed(self):
        from openmainty import util as _u
        with mock.patch.object(ic, "_run_gh",
                               return_value=canned(raw_issue())):
            first = ic.fetch_and_store("r", "run-x")
        obs_id = ("issue-856-"
                  + first["observation"]["content_fp"][:16])
        path = ic._slug_dir("o/r") / f"{obs_id}.json"
        kept = path.read_bytes()
        with mock.patch.object(ic, "_run_gh",
                               return_value=canned(raw_issue())):
            second = ic.fetch_and_store("r", "run-x")
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        self.assertEqual(first["observation"]["content_fp"],
                         second["observation"]["content_fp"])
        # identical content at a later time reuses the persisted bytes
        self.assertEqual(path.read_bytes(), kept)
        state = _u.read_json(ic.latest_path("o/r", 856), default=None)
        self.assertTrue(state["last_attempt"]["ok"])
        self.assertTrue(state["last_attempt"]["at"])

    def test_get_never_mutates(self):
        with mock.patch.object(ic, "_run_gh",
                               return_value=canned(raw_issue())):
            ic.fetch_and_store("r", "run-x")
        latest = ic.latest_path("o/r", 856)
        before = latest.read_bytes()
        with mock.patch.object(ic, "_run_gh",
                               side_effect=AssertionError("GET fetched")):
            ic.read_cached("r", "run-x")
            ic.read_cached("r", "run-x")
        self.assertEqual(latest.read_bytes(), before)

    def test_state_change_new_observation(self):
        with mock.patch.object(ic, "_run_gh",
                               return_value=canned(raw_issue())):
            first = ic.fetch_and_store("r", "run-x")
        with mock.patch.object(ic, "_run_gh",
                               return_value=canned(raw_issue(
                                   state="closed"))):
            second = ic.fetch_and_store("r", "run-x")
        self.assertTrue(second["changed"])
        self.assertEqual(second["observation"]["state"], "CLOSED")
        self.assertNotEqual(second["observation"]["content_fp"],
                            first["observation"]["content_fp"])

    def test_content_change_new_observation(self):
        with mock.patch.object(ic, "_run_gh",
                               return_value=canned(raw_issue())):
            first = ic.fetch_and_store("r", "run-x")
        with mock.patch.object(ic, "_run_gh",
                               return_value=canned(raw_issue(body="new"))):
            second = ic.fetch_and_store("r", "run-x")
        self.assertTrue(second["changed"])
        want_prev = ("issue-856-"
                     + first["observation"]["content_fp"][:16])
        self.assertEqual(second["previous_latest"], want_prev)

    def test_fetch_failure_records_attempt_keeps_prior(self):
        with mock.patch.object(ic, "_run_gh",
                               return_value=canned(raw_issue())):
            ic.fetch_and_store("r", "run-x")
        before = self.run_bytes()
        with mock.patch.object(
                ic, "_run_gh",
                side_effect=ic.IssueContextError("boom", status=502,
                                                 code="transport")):
            with self.assertRaises(ic.IssueContextError):
                ic.fetch_and_store("r", "run-x")
        self.assertEqual(self.run_bytes(), before)
        out = ic.read_cached("r", "run-x")
        self.assertTrue(out["cached"])
        self.assertIsNotNone(out["observation"])
        last = out["state"]["last_attempt"]
        self.assertFalse(last["ok"])
        self.assertIn("boom", last["error"])

    def test_corrupt_latest_is_error_state(self):
        latest = ic.latest_path("o/r", 856)
        latest.parent.mkdir(parents=True, exist_ok=True)
        latest.write_bytes(b"{broken")
        out = ic.read_cached("r", "run-x")
        self.assertFalse(out["cached"])
        self.assertIsNone(out["observation"])
        self.assertEqual(out["state"].get("store"), "error")

    def test_tampered_observation_refused_bytes_kept(self):
        with mock.patch.object(ic, "_run_gh",
                               return_value=canned(raw_issue())):
            res = ic.fetch_and_store("r", "run-x")
        obs_id = "issue-856-" + res["observation"]["content_fp"][:16]
        path = ic._slug_dir("o/r") / f"{obs_id}.json"
        before = path.read_bytes()
        path.write_bytes(b'{"tampered": true}')
        out = ic.read_cached("r", "run-x")
        self.assertFalse(out["cached"])
        self.assertEqual(out["state"].get("store"), "error")
        # tampered bytes stay in place for inspection (not healed)
        self.assertEqual(path.read_bytes(), b'{"tampered": true}')
        # latest pointer untouched
        state = json.loads(ic.latest_path("o/r", 856).read_bytes())
        self.assertEqual(state["latest"], obs_id)
        _ = before

    def test_missing_observation_pointer_is_error(self):
        from openmainty import util as _u
        _u.write_json(ic.latest_path("o/r", 856).parent /
                      "issue-856-latest.json",
                      {"latest": "issue-856-deadbeefdeadbeef",
                       "last_attempt": None})
        out = ic.read_cached("r", "run-x")
        self.assertFalse(out["cached"])
        self.assertEqual(out["state"].get("store"), "error")


class TestServeRoutes(unittest.TestCase):
    def setUp(self):
        make_home(self)
        fake_profile()
        fake_run()
        from openmainty import serve as _serve
        self.serve = _serve

    def test_get_missing_reads_unknown(self):
        out = self.serve.api_run_issue_content({"repo": "r",
                                                "run": "run-x"})
        self.assertTrue(out["applicable"])
        self.assertFalse(out["cached"])
        self.assertIsNone(out["observation"])

    def test_get_unknown_run_404(self):
        with self.assertRaises(self.serve.ApiError) as cm:
            self.serve.api_run_issue_content({"repo": "r",
                                              "run": "run-nope"})
        self.assertEqual(cm.exception.status, 404)

    def test_get_dep_run_applicable_false(self):
        from openmainty import store
        store.save({"schema": 1, "id": "run-dep", "repo": "r",
                    "issue": "dep-foo"})
        out = self.serve.api_run_issue_content({"repo": "r",
                                                "run": "run-dep"})
        self.assertFalse(out["applicable"])

    def test_fetch_requires_confirm(self):
        with self.assertRaises(self.serve.ApiError) as cm:
            self.serve.api_run_issue_content_fetch({"repo": "r",
                                                    "run": "run-x"})
        self.assertEqual(cm.exception.status, 400)

    def test_fetch_uses_exact_argv_and_stores(self):
        seen = []

        def fake_run_gh(argv, timeout=30):
            seen.append((argv, timeout))
            return json.dumps(raw_issue())

        with mock.patch.object(ic, "_run_gh", side_effect=fake_run_gh):
            out = self.serve.api_run_issue_content_fetch(
                {"repo": "r", "run": "run-x", "confirm": True})
        self.assertEqual(out["issue"], 856)
        self.assertTrue(out["changed"])
        self.assertEqual(seen, [(["gh", "issue", "view", "856",
                                  "--repo", "o/r", "--json",
                                  "number,title,body,state,url"], 30)])
        obs = out["observation"]
        self.assertEqual(obs["title"], "Web demo is not working")
        # second identical fetch reports unchanged
        with mock.patch.object(ic, "_run_gh",
                               return_value=json.dumps(raw_issue())):
            out2 = self.serve.api_run_issue_content_fetch(
                {"repo": "r", "run": "run-x", "confirm": True})
        self.assertFalse(out2["changed"])

    def test_fetch_busy_409_starts_nothing(self):
        # A live holder on a DIFFERENT thread must make the API refuse:
        # same-thread acquire is intentionally reentrant (sched.py), so
        # holding in this thread cannot simulate a busy worker.
        import threading
        from openmainty import sched
        held = threading.Event()
        release = threading.Event()
        outcome = {}

        def holder():
            token = sched.acquire_worker_lease(
                "issue-context-test-holder")
            outcome["token"] = token
            held.set()
            release.wait(timeout=30)
            sched.release_worker_lease(token)

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        try:
            self.assertTrue(held.wait(timeout=30))
            self.assertIsNotNone(outcome.get("token"))
            with mock.patch.object(ic, "_run_gh",
                                   side_effect=AssertionError("ran")):
                with self.assertRaises(self.serve.ApiError) as cm:
                    self.serve.api_run_issue_content_fetch(
                        {"repo": "r", "run": "run-x", "confirm": True})
            self.assertEqual(cm.exception.status, 409)
        finally:
            release.set()
            thread.join(timeout=30)
        self.assertFalse(thread.is_alive())
        out = ic.read_cached("r", "run-x")
        self.assertFalse(out["cached"])

    def test_fetch_dep_run_400(self):
        from openmainty import store
        store.save({"schema": 1, "id": "run-dep", "repo": "r",
                    "issue": "dep-foo"})
        with mock.patch.object(ic, "_run_gh",
                               side_effect=AssertionError("ran")):
            with self.assertRaises(self.serve.ApiError) as cm:
                self.serve.api_run_issue_content_fetch(
                    {"repo": "r", "run": "run-dep", "confirm": True})
        self.assertEqual(cm.exception.status, 400)

    def test_fetch_conflicting_client_source_rejected(self):
        bad_payloads = [
            {"repo": "r", "run": "run-x", "confirm": True, "issue": 857},
            {"repo": "r", "run": "run-x", "confirm": True,
             "issue": "857"},
            {"repo": "r", "run": "run-x", "confirm": True,
             "slug": "evil/x"},
            {"repo": "r", "run": "run-x", "confirm": True,
             "url": "https://github.com/o/r/issues/857"},
            {"repo": "r", "run": "run-x", "confirm": True,
             "issue": True},
        ]
        # NOTE: surrounding whitespace is strip-normalized, not a
        # mismatch, so "856 " is accepted as 856 by design.
        for payload in bad_payloads:
            with mock.patch.object(ic, "_run_gh",
                                   side_effect=AssertionError("ran")):
                with self.assertRaises(self.serve.ApiError) as cm:
                    self.serve.api_run_issue_content_fetch(payload)
            self.assertEqual(cm.exception.status, 400,
                             msg=repr(payload))

    def test_fetch_matching_client_source_accepted(self):
        with mock.patch.object(ic, "_run_gh",
                               return_value=canned(raw_issue())):
            out = self.serve.api_run_issue_content_fetch(
                {"repo": "r", "run": "run-x", "confirm": True,
                 "issue": 856, "slug": "o/r",
                 "url": "https://github.com/o/r/issues/856"})
        self.assertTrue(out["changed"])
        self.assertEqual(out["observation"]["number"], 856)

    def test_get_conflicting_client_source_rejected(self):
        with mock.patch.object(ic, "_run_gh",
                               side_effect=AssertionError("ran")):
            with self.assertRaises(self.serve.ApiError) as cm:
                self.serve.api_run_issue_content(
                    {"repo": "r", "run": "run-x", "slug": "evil/x"})
        self.assertEqual(cm.exception.status, 400)

    def test_traversal_repo_rejected(self):
        with self.assertRaises(self.serve.ApiError) as cm:
            self.serve.api_run_issue_content(
                {"repo": "../x", "run": "run-x"})
        self.assertEqual(cm.exception.status, 400)

    def test_traversal_run_rejected(self):
        with self.assertRaises(self.serve.ApiError) as cm:
            self.serve.api_run_issue_content_fetch(
                {"repo": "r", "run": "../../etc", "confirm": True})
        self.assertEqual(cm.exception.status, 400)

    def test_foreign_run_binding_rejected(self):
        from openmainty import repos
        repos.save({"name": "s", "github": "o/s", "baseBranch": "main",
                    "knowledge": [], "checks": [], "e2e": [],
                    "release": {}, "announce": {}})
        with mock.patch.object(ic, "_run_gh",
                               side_effect=AssertionError("ran")):
            with self.assertRaises(self.serve.ApiError) as cm:
                self.serve.api_run_issue_content(
                    {"repo": "s", "run": "run-x"})
        self.assertEqual(cm.exception.status, 400)

    def test_corrupt_observation_blocks_transport_and_preserves(self):
        # A corrupt cached observation must fail closed BEFORE any gh
        # call, even when the network would return changed content.
        with mock.patch.object(ic, "_run_gh",
                               return_value=canned(raw_issue())):
            out = self.serve.api_run_issue_content_fetch(
                {"repo": "r", "run": "run-x", "confirm": True})
        self.assertTrue(out["changed"])
        latest = ic.latest_path("o/r", 856)
        before = latest.read_bytes()
        obs_id = ("issue-856-" +
                  out["observation"]["content_fp"][:16])
        obs_path = ic._slug_dir("o/r") / f"{obs_id}.json"
        obs_path.write_bytes(b"{broken")
        calls = []

        def fake_gh(argv, timeout=30):
            calls.append(argv)
            return json.dumps(raw_issue(body="changed text"))

        with mock.patch.object(ic, "_run_gh", side_effect=fake_gh):
            with self.assertRaises(self.serve.ApiError) as cm:
                self.serve.api_run_issue_content_fetch(
                    {"repo": "r", "run": "run-x", "confirm": True})
        self.assertEqual(cm.exception.status, 502)
        self.assertEqual(calls, [])
        self.assertEqual(latest.read_bytes(), before)
        self.assertEqual(obs_path.read_bytes(), b"{broken")
        out = ic.read_cached("r", "run-x")
        self.assertFalse(out["cached"])

    def test_fetch_corrupt_state_preserves_bytes(self):
        latest = ic.latest_path("o/r", 856)
        latest.parent.mkdir(parents=True, exist_ok=True)
        latest.write_bytes(b"{broken")
        before = latest.read_bytes()
        with mock.patch.object(ic, "_run_gh",
                               return_value=canned(raw_issue())):
            with self.assertRaises(self.serve.ApiError) as cm:
                self.serve.api_run_issue_content_fetch(
                    {"repo": "r", "run": "run-x", "confirm": True})
        self.assertEqual(cm.exception.status, 502)
        self.assertEqual(latest.read_bytes(), before)


class TestHttpBoundary(unittest.TestCase):
    """Actual serve.Handler on an ephemeral loopback port.

    No live app/server/network: the handler serves the fixture HOME
    only, and gh transport stays mocked at the module boundary.
    """

    def setUp(self):
        make_home(self)
        fake_profile()
        fake_run()
        import threading
        from http.server import ThreadingHTTPServer
        from openmainty import serve as _serve
        self._serve = _serve
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                         _serve.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)

    def _stop_server(self):
        try:
            self.httpd.shutdown()
        except Exception:
            pass
        try:
            self.httpd.server_close()
        except Exception:
            pass
        self.thread.join(timeout=30)

    def _get(self, path):
        import json as _json
        import urllib.error
        import urllib.request
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d%s" % (self.port, path),
                    timeout=20) as resp:
                return resp.status, _json.loads(
                    resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, _json.loads(exc.read().decode("utf-8"))

    def _seed_success(self):
        with mock.patch.object(ic, "_run_gh",
                               return_value=canned(raw_issue())):
            out = self._serve.api_run_issue_content_fetch(
                {"repo": "r", "run": "run-x", "confirm": True})
        self.assertTrue(out["changed"])
        return out

    def test_http_get_unknown_is_200_never_fetched(self):
        status, body = self._get(
            "/api/run/issue-content?repo=r&run=run-x")
        self.assertEqual(status, 200)
        self.assertFalse(body["cached"])
        self.assertIsNone(body.get("observation"))

    def test_http_get_forwards_matching_identity(self):
        self._seed_success()
        status, body = self._get(
            "/api/run/issue-content?repo=r&run=run-x&slug=o/r")
        self.assertEqual(status, 200)
        self.assertTrue(body["cached"])
        self.assertEqual(body["observation"]["number"], 856)

    def test_http_get_forwards_conflicting_slug(self):
        self._seed_success()
        status, body = self._get(
            "/api/run/issue-content?repo=r&run=run-x&slug=evil/x")
        self.assertEqual(status, 400)

    def test_http_get_duplicate_conflicting_values_rejected(self):
        status, _body = self._get(
            "/api/run/issue-content?repo=r&run=run-x"
            "&slug=o/r&slug=evil/x")
        self.assertEqual(status, 400)
        status, body = self._get(
            "/api/run/issue-content?repo=r&run=run-x"
            "&slug=o/r&slug=o/r")
        self.assertEqual(status, 200)

    def test_http_get_wrong_issue_rejected(self):
        # HTTP carries strings only; "857" must mismatch like a bool 857
        # would at the direct API boundary (covered separately).
        self._seed_success()
        status, _body = self._get(
            "/api/run/issue-content?repo=r&run=run-x&issue=857")
        self.assertEqual(status, 400)

    def test_http_get_unknown_run_404(self):
        status, _body = self._get(
            "/api/run/issue-content?repo=r&run=nope")
        self.assertEqual(status, 404)


class TestStorageErrors(unittest.TestCase):
    def setUp(self):
        make_home(self)
        fake_profile()
        fake_run()

    def test_null_body_is_unreadable(self):
        with self.assertRaises(ic.IssueContextError):
            ic.validate_observation(raw_issue(body=None), "o/r", 856)

    def test_wrong_shaped_state_is_storage_error(self):
        from openmainty import util as _u
        for bad in ([1, 2],
                    {"latest": 856, "last_attempt": None},
                    {"latest": None, "last_attempt": "ok"},
                    {"latest": None, "last_attempt": {"at": "t",
                                                      "ok": "yes",
                                                      "error": ""}},
                    {"latest": "issue-857-abcdef0123456789",
                     "last_attempt": None},
                    {"latest": None}):
            _u.write_json(ic.latest_path("o/r", 856), bad)
            out = ic.read_cached("r", "run-x")
            self.assertFalse(out["cached"], msg=repr(bad))
            self.assertEqual(out["state"].get("store"), "error",
                             msg=repr(bad))
            self.assertIsNone(out["observation"], msg=repr(bad))

    def test_corrupt_state_blocks_overwrite_and_fetch(self):
        latest = ic.latest_path("o/r", 856)
        latest.parent.mkdir(parents=True, exist_ok=True)
        latest.write_bytes(b"{broken")
        before = latest.read_bytes()
        with mock.patch.object(ic, "_run_gh",
                               return_value=canned(raw_issue())):
            with self.assertRaises(ic.IssueContextError) as cm:
                ic.fetch_and_store("r", "run-x")
        self.assertEqual(cm.exception.code, "storage")
        # corrupt bytes preserved byte-for-byte, never healed or replaced
        self.assertEqual(latest.read_bytes(), before)

    def test_corrupt_state_with_transport_failure(self):
        latest = ic.latest_path("o/r", 856)
        latest.parent.mkdir(parents=True, exist_ok=True)
        latest.write_bytes(b"{broken")
        before = latest.read_bytes()
        with mock.patch.object(
                ic, "_run_gh",
                side_effect=ic.IssueContextError("net down", status=502,
                                                 code="transport")):
            with self.assertRaises(ic.IssueContextError) as cm:
                ic.fetch_and_store("r", "run-x")
        # storage failure surfaces (chained behind the fetch error),
        # corrupt bytes kept in place
        self.assertEqual(cm.exception.code, "storage")
        self.assertEqual(latest.read_bytes(), before)

    def test_symlink_latest_is_storage_error(self):
        target = ic.latest_path("o/r", 856).parent / "real.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"{}")
        link = ic.latest_path("o/r", 856)
        link.symlink_to(target)
        out = ic.read_cached("r", "run-x")
        self.assertFalse(out["cached"])
        self.assertEqual(out["state"].get("store"), "error")
        with mock.patch.object(ic, "_run_gh",
                               return_value=canned(raw_issue())):
            with self.assertRaises(ic.IssueContextError) as cm:
                ic.fetch_and_store("r", "run-x")
        self.assertEqual(cm.exception.code, "storage")
        self.assertEqual(target.read_bytes(), b"{}")

    def test_directory_in_obs_slot_is_storage_error(self):
        with mock.patch.object(ic, "_run_gh",
                               return_value=canned(raw_issue())):
            res = ic.fetch_and_store("r", "run-x")
        obs_id = "issue-856-" + res["observation"]["content_fp"][:16]
        path = ic._slug_dir("o/r") / f"{obs_id}.json"
        path.unlink()
        path.mkdir()
        out = ic.read_cached("r", "run-x")
        self.assertFalse(out["cached"])
        self.assertEqual(out["state"].get("store"), "error")
        self.assertTrue(path.is_dir())

    def test_symlinked_obs_file_never_followed(self):
        outside = ic.latest_path("o/r", 856).parent / "outside.json"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_bytes(b'{"foreign": true}')
        with mock.patch.object(ic, "_run_gh",
                               return_value=canned(raw_issue())):
            res = ic.fetch_and_store("r", "run-x")
        obs_id = "issue-856-" + res["observation"]["content_fp"][:16]
        path = ic._slug_dir("o/r") / f"{obs_id}.json"
        path.unlink()
        path.symlink_to(outside)
        out = ic.read_cached("r", "run-x")
        self.assertFalse(out["cached"])
        self.assertEqual(out["state"].get("store"), "error")
        self.assertEqual(outside.read_bytes(), b'{"foreign": true}')

    def test_last_success_binding_across_refetch_and_failure(self):
        # One deterministic clock covers EVERY operation with fixed
        # per-operation values: each successful fetch stamps
        # success-at then attempt-at; a failed fetch stamps one
        # attempt-at. The tail never exhausts mid-test.
        from openmainty import util as _u
        t_success1, t_attempt1 = "T-success-1", "T-attempt-1"
        t_success2, t_attempt2 = "T-success-2", "T-attempt-2"
        t_fail = "T-failure-1"
        clock = [t_success1, t_attempt1, t_success2, t_attempt2,
                 t_fail] + ["T-extra-%d" % i for i in range(100)]
        with mock.patch.object(_u, "utcnow", side_effect=clock):
            with mock.patch.object(ic, "_run_gh",
                                   return_value=canned(raw_issue())):
                first = ic.fetch_and_store("r", "run-x")
                self.assertTrue(first["changed"])
                obs_id = ("issue-856-" +
                          first["observation"]["content_fp"][:16])
                obs_path = ic._slug_dir("o/r") / f"{obs_id}.json"
                obs_bytes = obs_path.read_bytes()
                st1 = json.loads(
                    ic.latest_path("o/r", 856).read_bytes())
                # identical content at a later time: a new successful
                # fetch timestamp, same immutable observation bytes/id
                second = ic.fetch_and_store("r", "run-x")
            self.assertFalse(second["changed"])
            self.assertEqual(second["observation"]["content_fp"],
                             first["observation"]["content_fp"])
            self.assertEqual(obs_path.read_bytes(), obs_bytes)
            st2 = json.loads(ic.latest_path("o/r", 856).read_bytes())
            self.assertEqual(st2["last_success"]["obs"],
                             st1["last_success"]["obs"])
            self.assertNotEqual(st2["last_success"]["at"],
                                st1["last_success"]["at"])
            self.assertEqual(st1["last_success"]["at"], t_success1)
            self.assertEqual(st2["last_success"]["at"], t_success2)
            self.assertEqual(st2["last_attempt"]["at"], t_attempt2)
            # transport failure preserves the latest success binding
            # and immutable bytes; only the attempt record advances
            with mock.patch.object(ic, "_run_gh",
                                   side_effect=ic.IssueContextError(
                                       "net down", status=502,
                                       code="transport")):
                with self.assertRaises(ic.IssueContextError):
                    ic.fetch_and_store("r", "run-x")
        st3 = json.loads(ic.latest_path("o/r", 856).read_bytes())
        self.assertEqual(st3["last_success"], st2["last_success"])
        self.assertFalse(st3["last_attempt"]["ok"])
        self.assertIn("net down", st3["last_attempt"]["error"])
        self.assertEqual(st3["last_attempt"]["at"], t_fail)
        self.assertEqual(obs_path.read_bytes(), obs_bytes)
        out = ic.read_cached("r", "run-x")
        self.assertTrue(out["cached"])
        self.assertEqual(out["observation"]["content_fp"],
                         first["observation"]["content_fp"])

    def test_failure_without_prior_success_claims_no_last_success(self):
        with mock.patch.object(ic, "_run_gh",
                               side_effect=ic.IssueContextError(
                                   "net down", status=502,
                                   code="transport")):
            with self.assertRaises(ic.IssueContextError):
                ic.fetch_and_store("r", "run-x")
        out = ic.read_cached("r", "run-x")
        self.assertFalse(out["cached"])
        self.assertIsNone(out["observation"])
        self.assertNotIn("last_success", out["state"])
        self.assertFalse(out["state"]["last_attempt"]["ok"])

    def test_second_success_rebinds_last_success(self):
        from openmainty import util as _u
        ticks = ["T%d" % i for i in range(1, 100)]
        with mock.patch.object(ic, "_run_gh",
                               return_value=canned(raw_issue())), \
                mock.patch.object(_u, "utcnow", side_effect=ticks):
            first = ic.fetch_and_store("r", "run-x")
            with mock.patch.object(ic, "_run_gh",
                                   return_value=canned(raw_issue(
                                       body="fixed text"))):
                second = ic.fetch_and_store("r", "run-x")
        self.assertTrue(second["changed"])
        st = json.loads(ic.latest_path("o/r", 856).read_bytes())
        # first fetch stamped T1(success)/T2(attempt); the changed
        # second fetch stamps T3(success)/T4(attempt)
        self.assertEqual(st["last_success"]["at"], "T3")
        self.assertEqual(st["last_success"]["obs"],
                         "issue-856-" + second["observation"][
                             "content_fp"][:16])
        self.assertNotEqual(first["observation"]["content_fp"],
                            second["observation"]["content_fp"])

    def test_write_failure_preserves_prior_success(self):
        with mock.patch.object(ic, "_run_gh",
                               return_value=canned(raw_issue())):
            ic.fetch_and_store("r", "run-x")
        latest = ic.latest_path("o/r", 856)
        before = latest.read_bytes()
        # the state commit is an os.replace of a unique temp file;
        # faulting it must surface typed storage failure
        with mock.patch.object(ic, "_run_gh",
                               return_value=canned(raw_issue(
                                   body="new text"))), \
                mock.patch("os.replace",
                           side_effect=OSError("disk full")):
            with self.assertRaises(ic.IssueContextError) as cm:
                ic.fetch_and_store("r", "run-x")
        self.assertEqual(cm.exception.code, "storage")
        # prior success untouched; no healed or partial state
        self.assertEqual(latest.read_bytes(), before)
        out = ic.read_cached("r", "run-x")
        self.assertTrue(out["cached"])
        self.assertEqual(out["observation"]["body"],
                         "The demo shows a blank page.")

    def test_permission_error_read_is_storage_error(self):
        from openmainty import util as _u
        with mock.patch.object(ic, "_run_gh",
                               return_value=canned(raw_issue())):
            ic.fetch_and_store("r", "run-x")
        with mock.patch.object(_u, "read_json",
                               side_effect=PermissionError("denied")):
            out = ic.read_cached("r", "run-x")
        self.assertFalse(out["cached"])
        self.assertIsNone(out["observation"])
        self.assertEqual(out["state"].get("store"), "error")

    def test_dangling_last_success_is_storage_error(self):
        # last_success.obs pointing at a DIFFERENT same-issue content
        # id than latest is malformed: read must fail closed with
        # bytes untouched, never serve the mismatched body/timestamp.
        with mock.patch.object(ic, "_run_gh",
                               return_value=canned(raw_issue(
                                   body="aaa"))):
            out_a = ic.fetch_and_store("r", "run-x")
        with mock.patch.object(ic, "_run_gh",
                               return_value=canned(raw_issue(
                                   body="bbb"))):
            out_b = ic.fetch_and_store("r", "run-x")
        self.assertTrue(out_b["changed"])
        obs_a = ("issue-856-" +
                 out_a["observation"]["content_fp"][:16])
        obs_b = ("issue-856-" +
                 out_b["observation"]["content_fp"][:16])
        latest = ic.latest_path("o/r", 856)
        state = json.loads(latest.read_bytes())
        state["latest"] = obs_a
        state["last_success"] = {"at": state["last_success"]["at"],
                                 "obs": obs_b}
        latest.write_text(json.dumps(state, indent=2, sort_keys=True,
                                     ensure_ascii=True) + "\n",
                          encoding="utf-8")
        out = ic.read_cached("r", "run-x")
        self.assertFalse(out["cached"])
        self.assertIsNone(out["observation"])
        self.assertEqual(out["state"].get("store"), "error")

    def test_stored_bool_number_rejected(self):
        # A stored True must never pass as issue 1 (True == 1).
        # Baseline first: a real fetch_and_store of canonical
        # issue 1 caches a valid observation through the public path.
        from openmainty import store
        store.save({"schema": 1, "id": "run-1", "repo": "r",
                    "issue": "1"})
        url = "https://github.com/o/r/issues/1"
        with mock.patch.object(ic, "_run_gh",
                               return_value=canned(raw_issue(
                                   number=1, url=url))):
            first = ic.fetch_and_store("r", "run-1")
        self.assertTrue(first["changed"])
        out = ic.read_cached("r", "run-1")
        self.assertTrue(out["cached"])
        self.assertEqual(out["observation"]["number"], 1)
        # Corrupt ONLY the stored number with a bool; fingerprint,
        # provenance and every other field stay valid and untouched.
        obs_id = ic._obs_id(1, first["observation"]["content_fp"])
        obs_path = ic._slug_dir("o/r") / f"{obs_id}.json"
        persisted = json.loads(obs_path.read_bytes())
        persisted["number"] = True
        corrupt = json.dumps(persisted, indent=2, sort_keys=True,
                             ensure_ascii=True).encode("utf-8")
        obs_path.write_bytes(corrupt)
        out = ic.read_cached("r", "run-1")
        self.assertFalse(out["cached"])
        self.assertIsNone(out["observation"])
        self.assertEqual(out["state"].get("store"), "error")
        # the corrupt bytes stay in place: nothing healed or rewrote
        self.assertEqual(obs_path.read_bytes(), corrupt)

    def test_os_open_failure_is_typed_storage_and_preserves(self):
        with mock.patch.object(ic, "_run_gh",
                               return_value=canned(raw_issue())):
            ic.fetch_and_store("r", "run-x")
        latest = ic.latest_path("o/r", 856)
        before = latest.read_bytes()
        with mock.patch.object(ic, "_run_gh",
                               return_value=canned(raw_issue(
                                   body="new text"))), \
                mock.patch("os.open",
                           side_effect=PermissionError("denied")):
            with self.assertRaises(ic.IssueContextError) as cm:
                ic.fetch_and_store("r", "run-x")
        self.assertEqual(cm.exception.code, "storage")
        self.assertEqual(latest.read_bytes(), before)
        out = ic.read_cached("r", "run-x")
        self.assertTrue(out["cached"])
        self.assertEqual(out["observation"]["body"],
                         "The demo shows a blank page.")
