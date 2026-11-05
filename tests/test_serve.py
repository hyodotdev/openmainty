"""Durable coverage: dashboard API (chat actions, gate, jobs).

Spins the real HTTP server on an ephemeral loopback port with an isolated
OPENMAINTY_HOME. The Muse worker is patched to FakeExecutor and `gh` reads
are stubbed: no quota, no network. Run with:
  python3 -m unittest discover -s tests
"""
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import pcconfig, repos, runner, sched, serve, store  # noqa: E402
try:
    from tests._home import use_temp_home, offline_muse_version, offline_muse_binary, offline_service_status  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home, offline_muse_version, offline_muse_binary, offline_service_status  # noqa: E402

PROFILE = {
    "name": "fakerepo",
    "github": "example/fakerepo",
    "baseBranch": "main",
    "knowledge": [],
    "checks": [{"name": "unit", "cmd": ["make", "unit"], "requires": []}],
    "e2e": [],
    "release": {"notes": "CHANGELOG.md"},
    "announce": {"medium": False, "discord": False, "slack": False},
}


class ServeMixin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_temp_home(self, str(Path(self.tmp.name) / "home"))
        os.environ.pop("META_API_KEY", None)
        self.checkout = Path(self.tmp.name) / "checkout"
        self.checkout.mkdir()
        (self.checkout / "README.md").write_text("# fake\n")
        repos.save(dict(PROFILE))
        pc = pcconfig.load()
        pc["checkouts"]["fakerepo"] = str(self.checkout)
        pcconfig.save(pc)
        serve._JOBS.clear()
        serve._JOB_CANCEL.clear()
        # Generic behavior fixture has no real CLI version: stub metadata.
        offline_muse_version(self, "fixture-muse-0.0.0")
        # Generic behavior fixture must not depend on host PATH: stub
        # binary presence (the stub path is never invoked).
        offline_muse_binary(self, "/fixture/muse")
        patches = [
            mock.patch("openmainty.runner.Executor", runner.FakeExecutor),
            mock.patch("openmainty.runner.make_executor",
                       lambda name, model=None, cancel_event=None:
                       runner.FakeExecutor(cancel_event=cancel_event)),
            mock.patch("openmainty.cli._open_issues", return_value=[]),
            mock.patch("openmainty.cli._github_issue_body", return_value=""),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)

    def _stop_server(self):
        self.httpd.shutdown()
        self.thread.join(timeout=15)
        self.httpd.server_close()
        os.environ.pop("META_API_KEY", None)

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path):
        try:
            with urllib.request.urlopen(self.url(path), timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode() or "{}")

    def post(self, path, obj):
        req = urllib.request.Request(
            self.url(path), data=json.dumps(obj).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode() or "{}"
            try:
                return exc.code, json.loads(body)
            except ValueError:
                return exc.code, {"error": body[:200]}

    def poll_job(self, job_id, deadline_s=60):
        end = time.time() + deadline_s
        while time.time() < end:
            code, job = self.get(f"/api/job?id={job_id}")
            self.assertEqual(code, 200)
            if job["status"] != "running":
                return job
            time.sleep(1)
        self.fail(f"job {job_id} still running after {deadline_s}s")


class DashboardApiTests(ServeMixin):
    def test_onboard_caps_get_over_http(self):
        # M3.2b F1: the dashboard fetches this with GET. A direct
        # function call would pass even with no route, so this goes
        # through the real handler.
        code, body = self.get("/api/onboard/caps")
        self.assertEqual(code, 200)
        for key in ("scan_interval_s", "prepare_per_tick",
                    "refresh_per_tick", "refresh_per_day",
                    "rollup_per_lane_tick", "cooldown_blocks",
                    "cooldown_hours", "repo_limit",
                    "repo_limit_note"):
            self.assertIn(key, body)
        self.assertIsNone(body["repo_limit"])
        self.assertEqual(body["repo_limit_note"], "등록 수 제한 없음")

    def test_overview_shape(self):
        offline_service_status(self, {"installed": True, "running": False, "pid": None, "note": "fixture offline; scheduler discovery is not under test"})  # noqa: E501
        code, body = self.get("/api/overview")
        self.assertEqual(code, 200)
        for key in ("repos", "runs", "chats", "jobs", "watch_log_tail"):
            self.assertIn(key, body)
        self.assertEqual(body["repos"][0]["name"], "fakerepo")

    def test_overview_timing_breakdown(self):
        offline_service_status(self, {"installed": True, "running": False, "pid": None, "note": "fixture offline; scheduler discovery is not under test"})  # noqa: E501
        code, plain = self.get("/api/overview")
        self.assertEqual(code, 200)
        self.assertNotIn("timing", plain)
        code, body = self.get("/api/overview?timing=1")
        self.assertEqual(code, 200)
        timing = body.get("timing") or {}
        sections = timing.get("sections") or {}
        for name in ("repos", "meta", "batches", "runs_chats"):
            self.assertIn(name, sections)
            self.assertGreaterEqual(sections[name], 0)
        total = timing.get("total_ms", 0)
        self.assertGreaterEqual(total, 0)
        # Sections are per-section deltas: they sum to roughly the total
        # (the small remainder is final assembly after the last mark).
        self.assertLessEqual(sum(sections.values()), total)
        self.assertGreaterEqual(sum(sections.values()), total - 500)

    def test_overview_review_batches_from_snapshot(self):
        offline_service_status(self, {"installed": True, "running": False, "pid": None, "note": "fixture offline; scheduler discovery is not under test"})  # noqa: E501
        from openmainty import batches
        batches.refresh_batches_snapshot("test")
        snap = batches.read_json(batches.batches_snapshot_path(),
                                 default=None)
        code, body = self.get("/api/overview?timing=1")
        self.assertEqual(code, 200)
        self.assertEqual(body["review_batches"], snap["summary"])
        self.assertEqual(body["review_batches_obs"], snap["obs"])
        self.assertEqual(body["review_batches_at"], snap["at"])

    def test_overview_repo_rows_carry_glance_rollup(self):
        offline_service_status(self, {"installed": True, "running": False, "pid": None, "note": "fixture offline; scheduler discovery is not under test"})  # noqa: E501
        # M2.25b: the reviews tab reads r.rollup from /api/overview;
        # the rows come from the glance summary snapshot, read-only.
        from openmainty import batches
        from openmainty.util import write_json
        snap_path = batches.summary_snapshot_path()
        snap_path.parent.mkdir(parents=True, exist_ok=True)
        row = {"name": "fakerepo",
               "rollup": {"next": {"op": "rollup-next-fakerepo-1",
                                   "status": "done",
                                   "leaves": 2,
                                   "pr_title": "t",
                                   "pr_body": "b" * 10,
                                   "caveats": []}}}
        write_json(snap_path, {"repos": [row]})
        code, body = self.get("/api/overview")
        self.assertEqual(code, 200)
        mine = [r for r in body["repos"] if r["name"] == "fakerepo"]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["rollup"]["next"]["op"],
                         "rollup-next-fakerepo-1")
        self.assertEqual(mine[0]["rollup"]["next"]["pr_body"], "b" * 10)

    def test_overview_rollup_missing_without_snapshot(self):
        offline_service_status(self, {"installed": True, "running": False, "pid": None, "note": "fixture offline; scheduler discovery is not under test"})  # noqa: E501
        from openmainty import batches
        snap_path = batches.summary_snapshot_path()
        if snap_path.is_file():
            self.fail("snapshot exists; cannot test the degrade path")
        code, body = self.get("/api/overview")
        self.assertEqual(code, 200)
        mine = [r for r in body["repos"] if r["name"] == "fakerepo"]
        self.assertEqual(len(mine), 1)
        self.assertNotIn("rollup", mine[0])

    def test_overview_rollup_degrades_on_corrupt_snapshot(self):
        offline_service_status(self, {"installed": True, "running": False, "pid": None, "note": "fixture offline; scheduler discovery is not under test"})  # noqa: E501
        # M2.25c: read_json raises SystemExit on invalid JSON; the
        # overview must still return repo rows with no rollup key.
        from openmainty import batches
        snap_path = batches.summary_snapshot_path()
        snap_path.parent.mkdir(parents=True, exist_ok=True)
        snap_path.write_text("not json", encoding="utf-8")
        code, body = self.get("/api/overview")
        self.assertEqual(code, 200)
        mine = [r for r in body["repos"] if r["name"] == "fakerepo"]
        self.assertEqual(len(mine), 1)
        self.assertNotIn("rollup", mine[0])

    def test_overview_runs_are_summaries(self):
        offline_service_status(self, {"installed": True, "running": False, "pid": None, "note": "fixture offline; scheduler discovery is not under test"})  # noqa: E501
        store.create("fakerepo", "42", "some title", "base0", {})
        code, body = self.get("/api/overview")
        self.assertEqual(code, 200)
        self.assertTrue(body["runs"])
        allowed = {"id", "repo", "issue", "title", "dep", "target",
                   "lane", "branch", "created_at", "waitReason", "steps",
                   "disposition", "watch"}
        for run in body["runs"]:
            self.assertLessEqual(set(run), allowed)
            for entry in (run.get("steps") or {}).values():
                self.assertLessEqual(set(entry), {"status", "started_at"})
        blob = json.dumps(body["runs"])
        for heavy in ("evidence", "artifacts", "interventions",
                      "session_id", "workTreeFingerprint"):
            self.assertNotIn(heavy, blob)

    def test_overview_chats_are_summaries(self):
        offline_service_status(self, {"installed": True, "running": False, "pid": None, "note": "fixture offline; scheduler discovery is not under test"})  # noqa: E501
        from openmainty import chat
        started = self.post("/api/chat/start",
                            {"repo": "fakerepo", "message": "hello?"})
        self.assertEqual(started[0], 200)
        code, body = self.get("/api/overview")
        self.assertEqual(code, 200)
        self.assertTrue(body["chats"])
        allowed = {"id", "repo", "issue", "title", "run_id",
                   "created_at", "turns"}
        for sess in body["chats"]:
            self.assertLessEqual(set(sess), allowed)
            self.assertIsInstance(sess.get("turns"), int)
        blob = json.dumps(body["chats"])
        for heavy in ("transcript", "messages", "worker", "workdir_mode"):
            self.assertNotIn(heavy, blob)

    def test_overview_returns_all_runs_past_old_cap(self):
        offline_service_status(self, {"installed": True, "running": False, "pid": None, "note": "fixture offline; scheduler discovery is not under test"})  # noqa: E501
        for n in range(22):
            store.create("fakerepo", str(100 + n), "t", "base0", {})
        code, body = self.get("/api/overview")
        self.assertEqual(code, 200)
        self.assertEqual(len(body["runs"]), 22)

    def test_overview_returns_all_chats_past_old_cap(self):
        offline_service_status(self, {"installed": True, "running": False, "pid": None, "note": "fixture offline; scheduler discovery is not under test"})  # noqa: E501
        from openmainty import chat
        for n in range(51):
            chat.create("fakerepo", str(200 + n), f"t{n}", {})
        code, body = self.get("/api/overview")
        self.assertEqual(code, 200)
        self.assertEqual(len(body["chats"]), 51)

    def test_chat_start_and_send_roundtrip(self):
        code, started = self.post("/api/chat/start",
                                  {"repo": "fakerepo", "message": "hello?"})
        self.assertEqual(code, 200)
        self.assertIn("fake chat ok", started["reply"])
        code, sent = self.post("/api/chat/send",
                               {"id": started["id"], "message": "again"})
        self.assertEqual(code, 200)
        self.assertIn("fake chat ok", sent["reply"])
        code, sess = self.get(f"/api/session?id={started['id']}")
        self.assertEqual(code, 200)
        self.assertEqual([t["role"] for t in sess["transcript"]],
                         ["user", "assistant", "user", "assistant"])

    def test_chat_start_requires_repo(self):
        code, body = self.post("/api/chat/start", {"message": "hi"})
        self.assertEqual(code, 400)

    def test_chat_unknown_repo_and_session(self):
        code, _ = self.post("/api/chat/start", {"repo": "nope", "message": "hi"})
        self.assertEqual(code, 404)
        code, _ = self.post("/api/chat/send", {"id": "chat-deadbeef", "message": "hi"})
        self.assertEqual(code, 404)

    def test_subscriber_gate_blocks_quota_endpoints(self):
        os.environ["META_API_KEY"] = "test-not-a-real-key"
        for path, obj in [
                ("/api/chat/start", {"repo": "fakerepo", "message": "hi"}),
                ("/api/chat/send", {"id": "x", "message": "hi"}),
                ("/api/job/maintain", {"repo": "fakerepo"}),
                ("/api/job/run", {"repo": "fakerepo", "issue": "1"}),
                ("/api/job/resume", {"run": "run-deadbeef"})]:
            code, body = self.post(path, obj)
            self.assertEqual(code, 403, path)
        # the drift/issue check spends no quota, so it stays open
        code, body = self.post("/api/watch", {"repo": "fakerepo"})
        self.assertEqual(code, 200)

    def test_subscriber_gate_blocks_without_muse_binary(self):
        from openmainty import muse as _muse
        with mock.patch.object(_muse, "which", return_value=None):
            for path, obj in [
                    ("/api/chat/start", {"repo": "fakerepo", "message": "hi"}),
                    ("/api/job/maintain", {"repo": "fakerepo"})]:
                code, body = self.post(path, obj)
                self.assertEqual(code, 403, path)
                self.assertIn("muse", body.get("error", ""))

    def test_quota_free_gets_answer_200_without_muse_binary(self):
        # F1: summary/app/digest/qa-pack-get read stored bytes only, so
        # they stay open while quota-spending endpoints refuse.
        from openmainty import muse as _muse
        with mock.patch.object(_muse, "which", return_value=None):
            code, body = self.get("/api/summary")
            self.assertEqual(code, 200)
            sub = body.get("subscription") or {}
            self.assertTrue(sub.get("login_needed"))
            code, _ = self.get("/api/app")
            self.assertEqual(code, 200)
            code, _ = self.get("/api/digest")
            self.assertEqual(code, 200)
            # No stored pack: 400/404 JSON, but never the 403 gate.
            code, _ = self.get("/api/batch/qa-pack?batch=nope")
            self.assertNotEqual(code, 403)
            # A quota POST still refuses on the same binary-less host.
            code, body = self.post(
                "/api/job/answer",
                {"run": "run-deadbeef", "answer": "x"})
            self.assertEqual(code, 403)

    def test_summary_subscription_reason_cli_missing_and_api_key_set(self):
        # F7: the ungated hint names the honest remedy. Presence only —
        # the key value must never appear in the response.
        import json

        from openmainty import muse as _muse
        with mock.patch.object(_muse, "which", return_value=None):
            code, body = self.get("/api/summary")
            self.assertEqual(code, 200)
            sub = body.get("subscription") or {}
            self.assertTrue(sub.get("login_needed"))
            self.assertEqual(sub.get("reason"), "cli_missing")
        os.environ["META_API_KEY"] = "test-not-a-real-key"
        code, body = self.get("/api/summary")
        self.assertEqual(code, 200)
        sub = body.get("subscription") or {}
        self.assertTrue(sub.get("login_needed"))
        self.assertEqual(sub.get("reason"), "api_key_set")
        self.assertNotIn("test-not-a-real-key", json.dumps(body))
        # Quota POSTs keep refusing, with the api_key wording first.
        code, body = self.post(
            "/api/job/answer", {"run": "run-deadbeef", "answer": "x"})
        self.assertEqual(code, 403)
        self.assertIn("META_API_KEY is set", body.get("error", ""))

    def test_watch_reports_issues_with_decisions(self):
        with mock.patch("openmainty.cli._open_issues",
                        return_value=[{"number": 7, "title": "fake bug"}]):
            code, body = self.post("/api/watch", {"repo": "fakerepo"})
        self.assertEqual(code, 200)
        # Manifest-less fixture checkout: zero real dependencies checked;
        # the "(manifest)" note is a visible warning, not a check.
        # "No changes" must never mean undetected.
        self.assertEqual(body["checked"], 0)
        self.assertEqual(body["warnings"], 1)
        self.assertTrue(body["unsupported"])
        self.assertEqual(body["drift"], [])
        self.assertEqual(body["errors"], [])
        self.assertEqual(body["issues"],
                         [{"number": "7", "title": "fake bug", "decision": "start",
                           "session_id": None, "detail": "", "question": ""}])

    def test_watch_links_session_and_detail(self):
        from openmainty import chat
        state = store.create("fakerepo", "7", "fake bug", "base0", {})
        for step in store.STEP_ORDER:
            store.mark(state, step, "done", note="1 check(s) passed")
        session = chat.sync_run_session(store.load(state["id"]))
        with mock.patch("openmainty.cli._open_issues",
                        return_value=[{"number": 7, "title": "fake bug"}]):
            code, body = self.post("/api/watch", {"repo": "fakerepo"})
        self.assertEqual(code, 200)
        self.assertEqual(body["issues"][0]["decision"], "skip-done")
        self.assertEqual(body["issues"][0]["session_id"], session["id"])
        self.assertIn("완료", body["issues"][0]["detail"])

    def _git_repo(self, name, remote=None, branch="main"):
        from openmainty.util import run_cmd
        d = Path(self.tmp.name) / name
        d.mkdir()
        (d / "README.md").write_text("# t\n")
        run_cmd(["git", "-C", str(d), "init"])
        run_cmd(["git", "-C", str(d), "add", "."])
        run_cmd(["git", "-C", str(d), "-c", "user.email=t@t", "-c", "user.name=t",
                 "commit", "-m", "init"])
        run_cmd(["git", "-C", str(d), "branch", "-M", branch])
        if remote:
            run_cmd(["git", "-C", str(d), "remote", "add", "origin", remote])
        return d

    def test_repo_add_local_roundtrip(self):
        offline_service_status(self, {"installed": True, "running": False, "pid": None, "note": "fixture offline; scheduler discovery is not under test"})  # noqa: E501
        d = self._git_repo("localproj", remote="https://github.com/example/localproj.git")
        code, body = self.post("/api/repo/add-local", {"path": str(d)})
        self.assertEqual(code, 200)
        self.assertEqual(body["repo"]["name"], "localproj")
        self.assertEqual(body["repo"]["github"], "example/localproj")
        self.assertEqual(body["repo"]["baseBranch"], "main")
        code, over = self.get("/api/overview")
        self.assertIn("localproj", [r["name"] for r in over["repos"]])
        code, body = self.post("/api/repo/delete", {"repo": "localproj"})
        self.assertEqual(code, 200)
        self.assertTrue(d.is_dir(), "delete must keep checkout files")

    def test_repo_add_local_errors(self):
        code, _ = self.post("/api/repo/add-local", {"path": "/no/such/dir"})
        self.assertEqual(code, 400)
        code, _ = self.post("/api/repo/add-local", {"path": "relative/path"})
        self.assertEqual(code, 400)
        plain = Path(self.tmp.name) / "notgit"
        plain.mkdir()
        code, body = self.post("/api/repo/add-local", {"path": str(plain)})
        self.assertEqual(code, 400)
        self.assertIn("not a git repository", body["error"])
        d = self._git_repo("plain")
        code, _ = self.post("/api/repo/add-local", {"path": str(d)})
        self.assertEqual(code, 400)
        code, _ = self.post("/api/repo/add-local",
                            {"path": str(d), "github": "ex/plain", "name": "../evil"})
        self.assertEqual(code, 400)
        code, _ = self.post("/api/repo/add-local",
                            {"path": str(d), "github": "ex/plain", "name": "fakerepo"})
        self.assertEqual(code, 409)

    def test_repo_delete_guards(self):
        code, _ = self.post("/api/repo/delete", {"repo": "nope"})
        self.assertEqual(code, 404)
        from openmainty import store
        state = store.create("fakerepo", "8", "t", "base0", {})
        store.claim("fakerepo", "8", state["id"])
        code, _ = self.post("/api/repo/delete", {"repo": "fakerepo"})
        self.assertEqual(code, 409)

    def test_settings_roundtrip(self):
        code, body = self.get("/api/settings")
        self.assertEqual(code, 200)
        self.assertTrue(body["workspace_root"].endswith("/openmainty"))
        target = str(Path(self.tmp.name) / "ws")
        code, body = self.post("/api/settings", {"workspace_root": target})
        self.assertEqual(code, 200)
        self.assertEqual(body["workspace_root"], target)
        self.assertTrue(Path(target).is_dir())
        code, _ = self.post("/api/settings", {"workspace_root": "relative"})
        self.assertEqual(code, 400)
        code, _ = self.post("/api/settings", {})
        self.assertEqual(code, 400)

    def test_settings_model_roundtrip(self):
        code, body = self.get("/api/settings")
        self.assertEqual(body["model"], "muse-spark-1.3")
        code, body = self.post("/api/settings", {"model": "muse-spark-9.9"})
        self.assertEqual(code, 200)
        self.assertEqual(body["model"], "muse-spark-9.9")
        code, started = self.post("/api/chat/start", {"repo": "fakerepo"})
        self.assertEqual(code, 200)
        code, sess = self.get(f"/api/session?id={started['id']}")
        self.assertEqual(sess["session"]["worker"]["model"], "muse-spark-9.9")
        code, _ = self.post("/api/settings", {"model": "bad model!"})
        self.assertEqual(code, 400)

    def test_clone_validates_before_running(self):
        code, _ = self.post("/api/job/clone", {"github": "not-a-slug"})
        self.assertEqual(code, 400)
        # M3.2: clones land in app-owned checkouts/<owner>/<repo>, so
        # the taken-dir refusal moved there too (same 409 behavior).
        taken = (Path(os.environ["OPENMAINTY_HOME"]) / "checkouts" /
                 "example" / "taken")
        taken.mkdir(parents=True)
        code, _ = self.post("/api/job/clone", {"github": "example/taken"})
        self.assertEqual(code, 409)

    def test_browse_lists_dirs(self):
        root = Path(self.tmp.name) / "fs"
        (root / "proj" / ".git").mkdir(parents=True)
        (root / ".hidden").mkdir()
        (root / "file.txt").write_text("x")
        code, body = self.get(f"/api/browse?path={urllib.parse.quote(str(root))}")
        self.assertEqual(code, 200)
        self.assertEqual(body["path"], str(root.resolve()))
        proj = [e for e in body["entries"] if e["name"] == "proj"][0]
        self.assertTrue(proj["is_dir"] and proj["is_git"])
        self.assertNotIn(".hidden", [e["name"] for e in body["entries"]])
        code, body = self.get(f"/api/browse?path={urllib.parse.quote(str(root))}&hidden=1")
        self.assertIn(".hidden", [e["name"] for e in body["entries"]])
        code, _ = self.get("/api/browse?path=/no/such/dir")
        self.assertEqual(code, 404)
        code, _ = self.get(f"/api/browse?path={urllib.parse.quote(str(root / 'file.txt'))}")
        self.assertEqual(code, 404)

    def test_clone_repo_helper_local(self):
        src = self._git_repo("srcrepo")
        dest = Path(self.tmp.name) / "ws3" / "srcrepo"
        serve._clone_repo(str(src), dest)
        self.assertTrue((dest / "README.md").is_file())

    def _post_stream(self, obj):
        req = urllib.request.Request(
            self.url("/api/chat/stream"), data=json.dumps(obj).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/event-stream", resp.headers.get("Content-Type", ""))
            return resp.read().decode()

    @staticmethod
    def _frames(body):
        events = []
        for frame in body.split("\n\n"):
            for line in frame.split("\n"):
                if line.startswith("data:"):
                    events.append(json.loads(line[5:].strip()))
        return events

    def test_chat_stream_roundtrip(self):
        events = self._frames(self._post_stream({"repo": "fakerepo", "message": "hi?"}))
        self.assertEqual([e["t"] for e in events], ["started", "chunk", "done"])
        self.assertIn("fake chat ok", events[1]["text"])
        self.assertIn("fake chat ok", events[2]["reply"])
        sid = events[0]["id"]
        events = self._frames(self._post_stream({"id": sid, "message": "again"}))
        self.assertEqual([e["t"] for e in events], ["started", "chunk", "done"])

    def test_chat_stream_wait_and_gate(self):
        os.environ["OPENMAINTY_FAKE_QUOTA"] = "chat"
        try:
            events = self._frames(self._post_stream({"repo": "fakerepo", "message": "hi"}))
        finally:
            os.environ.pop("OPENMAINTY_FAKE_QUOTA", None)
        self.assertEqual([e["t"] for e in events], ["started", "wait"])
        os.environ["META_API_KEY"] = "test-not-a-real-key"
        try:
            req = urllib.request.Request(
                self.url("/api/chat/stream"), data=json.dumps(
                    {"repo": "fakerepo", "message": "hi"}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            try:
                urllib.request.urlopen(req, timeout=30)
                self.fail("expected 403")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 403)
        finally:
            os.environ.pop("META_API_KEY", None)

    def test_plan_flow_approve_runs_maintain(self):
        code, body = self.post("/api/plan/start", {"repo": "fakerepo"})
        self.assertEqual(code, 200)
        sid = body["id"]
        code, _ = self.post("/api/plan/start", {})
        self.assertEqual(code, 400)
        events = self._frames(self._post_stream({"id": sid, "plan": True}))
        self.assertEqual([e["t"] for e in events], ["started", "chunk", "done"])
        self.assertIn("fake chat ok", events[2]["reply"])
        code, sess = self.get(f"/api/session?id={sid}")
        self.assertEqual(code, 200)
        self.assertEqual(sess["session"].get("plan_pending"), "fakerepo")
        self.assertEqual([t["role"] for t in sess["transcript"]], ["assistant"])
        code, body = self.post("/api/plan/approve", {"id": sid})
        self.assertEqual(code, 200)
        job = self.poll_job(body["job"]["id"])
        self.assertEqual(job["rc"], 0)
        self.assertEqual(job["kind"], "maintain")
        code, sess = self.get(f"/api/session?id={sid}")
        self.assertNotIn("plan_pending", sess["session"])
        code, _ = self.post("/api/plan/approve", {"id": sid})
        self.assertEqual(code, 409)

    def test_run_stream_tails_live_log(self):
        state = store.create("fakerepo", "9", "t", "base0", {})
        store.mark(state, "triage", "running")
        store.save(state)
        live = Path(store.run_dir(state["id"])) / "triage.live.log.txt"
        live.write_text("hello ", encoding="utf-8")
        frames = []

        def reader():
            req = urllib.request.Request(self.url(f"/api/run/stream?id={state['id']}"))
            with urllib.request.urlopen(req, timeout=30) as resp:
                for raw in resp:
                    line = raw.decode()
                    if line.startswith("data:"):
                        frames.append(json.loads(line[5:]))
                        if frames[-1].get("t") == "done":
                            break

        t = threading.Thread(target=reader)
        t.start()
        time.sleep(1.5)
        with live.open("a", encoding="utf-8") as fh:
            fh.write("world")
        time.sleep(1.5)
        st = store.load(state["id"])
        store.mark(st, "triage", "done")
        store.save(st)
        t.join(timeout=30)
        kinds = [f["t"] for f in frames]
        self.assertIn("step", kinds)
        self.assertIn("chunk", kinds)
        self.assertEqual(kinds[-1], "done")
        self.assertIn("world", "".join(f.get("text", "") for f in frames))

    def test_run_stream_done_when_idle(self):
        state = store.create("fakerepo", "10", "t", "base0", {})
        req = urllib.request.Request(self.url(f"/api/run/stream?id={state['id']}"))
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode()
        self.assertIn('"done"', body)

    def test_job_answer_resumes_question(self):
        profile = repos.load("fakerepo")
        state = store.create("fakerepo", "3", "t", "base0", {})
        work = Path(self.tmp.name) / "askwork"
        work.mkdir()
        ex = runner.FakeExecutor(
            script={"triage": [(0, "notes\nOPENMAINTY_ASK: which API version?")]})
        self.assertFalse(runner.run_worker_step(state, profile, "triage", ex, work, "body"))
        self.assertEqual(state["waitReason"], "question: which API version?")
        code, body = self.post("/api/job/answer", {"run": state["id"], "answer": "v2"})
        self.assertEqual(code, 200)
        job = self.poll_job(body["job"]["id"])
        self.assertEqual(job["rc"], 0)
        code, run = self.get(f"/api/run?id={state['id']}")
        self.assertEqual(code, 200)
        self.assertIsNone(run["waitReason"])
        self.assertEqual(run["answers"], [{"q": "which API version?", "a": "v2"}])
        self.assertEqual(run["steps"]["triage"]["status"], "done")
        code, _ = self.post("/api/job/answer", {"run": state["id"], "answer": "x"})
        self.assertEqual(code, 409)

    def test_job_run_completes_fake_run(self):
        offline_service_status(self, {"installed": True, "running": False, "pid": None, "note": "fixture offline; scheduler discovery is not under test"})  # noqa: E501
        code, body = self.post("/api/job/run", {"repo": "fakerepo", "issue": "1"})
        self.assertEqual(code, 200)
        job = self.poll_job(body["job"]["id"])
        self.assertEqual(job["rc"], 0)
        code, over = self.get("/api/overview")
        runs = [r for r in over["runs"] if r["repo"] == "fakerepo" and r["issue"] == "1"]
        self.assertEqual(len(runs), 1)
        self.assertTrue(all(e["status"] == "done" for e in runs[0]["steps"].values()))

    def test_job_run_refuses_finished_issue(self):
        code, body = self.post("/api/job/run", {"repo": "fakerepo", "issue": "9"})
        self.assertEqual(code, 200)
        self.poll_job(body["job"]["id"])
        code, body = self.post("/api/job/run", {"repo": "fakerepo", "issue": "9"})
        self.assertEqual(code, 409)

    def test_job_serialization_unit(self):
        first = serve._job_new("maintain", "a")
        self.assertIsNotNone(first)
        self.assertIsNotNone(serve._JOB_CANCEL.get(first["id"]))
        self.assertIsNone(serve._job_new("maintain", "b"))
        first["status"] = "done"
        self.assertIsNotNone(serve._job_new("maintain", "c"))
        serve._JOBS.clear()
        serve._JOB_CANCEL.clear()

    def test_job_cancel_running_job_then_resume(self):
        offline_service_status(self, {"installed": True, "running": False, "pid": None, "note": "fixture offline; scheduler discovery is not under test"})  # noqa: E501
        os.environ["OPENMAINTY_FAKE_BLOCK"] = "1"
        try:
            code, body = self.post("/api/job/run", {"repo": "fakerepo", "issue": "3"})
            self.assertEqual(code, 200)
            jid = body["job"]["id"]
            code, job = self.get(f"/api/job?id={jid}")
            self.assertEqual(job["status"], "running")
            code, body = self.post("/api/job/cancel", {"id": jid})
            self.assertEqual(code, 200)
            job = self.poll_job(jid)
            self.assertEqual(job["status"], "cancelled")
            self.assertEqual(job["rc"], 130)
        finally:
            os.environ.pop("OPENMAINTY_FAKE_BLOCK", None)
        code, over = self.get("/api/overview")
        run_id = [r["id"] for r in over["runs"]
                  if r["repo"] == "fakerepo" and r["issue"] == "3"][0]
        code, body = self.post("/api/job/resume", {"run": run_id})
        self.assertEqual(code, 200)
        job = self.poll_job(body["job"]["id"])
        self.assertEqual(job["rc"], 0)

    def test_job_cancel_not_running(self):
        code, _ = self.post("/api/job/cancel", {"id": "job-deadbeef"})
        self.assertEqual(code, 404)
        code, body = self.post("/api/job/run", {"repo": "fakerepo", "issue": "4"})
        self.assertEqual(code, 200)
        self.poll_job(body["job"]["id"])
        code, _ = self.post("/api/job/cancel", {"id": body["job"]["id"]})
        self.assertEqual(code, 409)

    def test_index_serves_chat_first_ui(self):
        with urllib.request.urlopen(self.url("/"), timeout=30) as resp:
            self.assertEqual(resp.status, 200)
            html_text = resp.read().decode()
        for marker in ("유지보수 계획 세우기", "승인하고 실행", "저장소를 등록하세요",
                       "새 대화", "최근 대화", "실행 기록",
                       "/api/chat/start", "/api/chat/stream", "/api/job/run",
                       "/api/plan/start", "/api/plan/approve", "/api/job/answer",
                       "/api/job/cancel", "/api/repo/add-local", "/api/settings"):
            self.assertIn(marker, html_text)

    def test_chat_delete_removes_session(self):
        code, started = self.post("/api/chat/start",
                                  {"repo": "fakerepo", "message": "hi"})
        self.assertEqual(code, 200)
        code, body = self.post("/api/chat/delete", {"id": started["id"]})
        self.assertEqual(code, 200)
        self.assertEqual(body["deleted"], started["id"])
        code, _ = self.get(f"/api/session?id={started['id']}")
        self.assertEqual(code, 404)
        code, _ = self.post("/api/chat/delete", {"id": started["id"]})
        self.assertEqual(code, 404)

    def test_html_ids_resolve(self):
        """Every $("id") lookup must have a matching id= definition.

        Guards the blank-page class of bug: one missing id= throws at
        script top level and kills the whole dashboard.
        """
        import re
        src = (Path(__file__).resolve().parent.parent / "src" / "openmainty"
               / "dashboard.html").read_text(encoding="utf-8")
        defined = set(re.findall(r'id="([a-z-]+)"', src))
        used = set(re.findall(r'\$\("([a-z-]+)"\)', src))
        self.assertTrue(used, "no lookups found; regex broken?")
        self.assertEqual(used - defined, set())

    def test_unknown_post_path_and_bad_json(self):
        code, _ = self.post("/api/nope", {})
        self.assertEqual(code, 404)
        req = urllib.request.Request(
            self.url("/api/watch"), data=b"{not json",
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=30)
            self.fail("expected 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)


class DashboardStaticContractTests(unittest.TestCase):
    """Server-free dashboard contracts (also runnable where loopback is blocked).

    Mirrors the served-markers test statically and pins the UI truthfulness
    fixes: no NUL bytes, native sidebar button semantics, keyed dep review
    without execution, container-adaptive graph without a pan disclaimer on
    the default overview.

    Failure text is bounded on purpose: asserting directly against the whole
    dashboard source would dump ~250KB of HTML on every failure, so these
    helpers report only the missing token.
    """

    @staticmethod
    def src():
        return (Path(__file__).resolve().parent.parent / "src" / "openmainty"
                / "dashboard.html").read_text(encoding="utf-8")

    def assertHas(self, src, token):
        self.assertTrue(token in src, f"dashboard missing {token!r}")

    def assertLacks(self, src, token):
        self.assertFalse(token in src, f"dashboard still has {token!r}")

    def test_no_nul_bytes(self):
        raw = (Path(__file__).resolve().parent.parent / "src" / "openmainty"
               / "dashboard.html").read_bytes()
        self.assertNotIn(b"\x00", raw)

    def test_id_lookups_resolve_statically(self):
        src = self.src()
        defined = set(re.findall(r'id="([a-z-]+)"', src))
        used = set(re.findall(r'\$\("([a-z-]+)"\)', src))
        self.assertTrue(used, "no lookups found; regex broken?")
        self.assertEqual(used - defined, set())

    def test_served_markers_present(self):
        src = self.src()
        for marker in ("유지보수 계획 세우기", "승인하고 실행", "저장소를 등록하세요",
                       "새 대화", "최근 대화", "실행 기록",
                       "/api/chat/start", "/api/chat/stream", "/api/job/run",
                       "/api/plan/start", "/api/plan/approve", "/api/job/answer",
                       "/api/job/cancel", "/api/repo/add-local", "/api/settings"):
            self.assertHas(src, marker)

    def test_sidebar_keeps_button_semantics(self):
        src = self.src()
        self.assertHas(src, '<ul id="repolist"')
        self.assertLacks(src, 'role="listitem"')

    def test_dep_exact_key_review_and_execution(self):
        src = self.src()
        seg = src[src.index("async function paintRepoDeps"):]
        seg = seg[:seg.index("async function paintRepoBranches")]
        self.assertHas(seg, "data-depkey")
        self.assertHas(seg, "제안 키")
        # review opens only that key's panel; execution posts exactly that key
        self.assertHas(seg, "실행 검토")
        self.assertHas(seg, 'jpost("/api/sched/run-due", {repo, key, fake: false})')
        self.assertHas(seg, "실행 거부")
        # stale no-keyed-API assumption is gone; no generic whole-repo calls here
        self.assertLacks(seg, "API는 아직 없습니다")
        self.assertLacks(seg, "startPlan(")
        self.assertLacks(seg, 'jpost("/api/sched/run-due", {repo: r.name, fake')

    def test_dep_accordion_progressive_disclosure(self):
        src = self.src()
        seg = src[src.index("async function paintRepoDeps"):]
        seg = seg[:seg.index("async function paintRepoBranches")]
        # one details accordion per package, summary-first, evidence inside
        self.assertHas(seg, "details class=\"depcard\" data-deppkg")
        self.assertHas(seg, "<summary>")
        # registry link renders per actual manager (npm/pub.dev/unknown);
        # the literal labels live in registryUrl, pinned by
        # test_registry_links_per_manager
        self.assertHas(seg, "${reg.label}")
        self.assertHas(seg, "data-depexec")
        # expansion / filter / review survive polling repaints
        self.assertHas(seg, "depOpenPkgs")
        self.assertHas(seg, "depOpenRev")
        self.assertHas(seg, "depFilterQ")

    def test_chat_related_refs_title_first(self):
        src = self.src()
        seg = src[src.index("function summaryCard"):]
        seg = seg[:seg.index("async function openDraft")]
        # actual title is the main label, number secondary; via is provenance
        self.assertHas(seg, "관련 변경·언급")
        self.assertHas(seg, "언급 출처")
        self.assertHas(seg, "제목 미확인")
        self.assertHas(seg, "대화 열기")
        self.assertLacks(seg, '<span class="via">')
        # inline bare #NNN refs become titled links, never forged without a slug
        self.assertHas(src, "function linkifyIssueRefs")
        self.assertHas(src, "이슈 #${m[1]} 열기")

    def test_dep_rows_show_provenance_not_placeholders(self):
        src = self.src()
        self.assertHas(src, "DEP_SKIP_KIND")
        self.assertHas(src, "변경 없음")
        self.assertHas(src, "로컬·수동 관리 — 외부 업그레이드 대상 아님")
        self.assertLacks(src, "범위 충족 여부는 스캔 시 대조")

    def test_graph_adapts_to_container(self):
        src = self.src()
        for token in ("ovNarrow", "fitGraphChrome", "gstack",
                      "getBoundingClientRect", "gLabelW"):
            self.assertHas(src, token)
        # layout is measured only after the empty inspector yields its width
        self.assertHas(src, "graphCanvasW")
        # main column too narrow for side-by-side panels stacks the inspector
        self.assertHas(src, "mainW < 1000")

    def test_graph_reads_at_one_to_one_scale(self):
        src = self.src()
        # overview aggregate labels are always short category labels;
        # the repo name appears on the root node only, never as a prefix.
        # The exact label construction lives in buildAggView (covered by the
        # pure graph-poll behavior test); here we pin the wiring, not the
        # stale object-literal shape.
        self.assertHas(src, "AGG_KO[t]")
        self.assertHas(src, "${list.length}건")
        self.assertHas(src, "aggBranchSummary(list, edges)")
        self.assertLacks(src, "shortRepo(r)} · ${AGG_KO")
        # initial fit is 1:1: viewBox width equals CSS pixels, height grows
        # with content instead of shrinking into a fixed box
        self.assertHas(src, "w: panelW")
        self.assertHas(src, "svg.style.height")
        self.assertHas(src, "__graphMetrics")
        self.assertHas(src, "projectedFont")
        # no shrink-to-fit disclaimer anywhere: small text is never excused
        self.assertLacks(src, "전체가 패널보다 넓어 글자 크기를 지켰습니다")

    def test_s18_fastcheck_button_and_isolation_line(self):
        src = self.src()
        # S18b: a done op with no receipt offers one explicit check.
        self.assertHas(src, 'id="prep-fc-go"')
        self.assertHas(src, "검증 실행")
        self.assertHas(src, "/api/integration/fastcheck")
        # S18d: install steps say so honestly, in the exact words.
        self.assertHas(src, "파일 격리 · 네트워크 허용(설치)")
        self.assertHas(src, "function fcIsolationLine")

    def test_m32_onboarding_wizard_contract(self):
        src = self.src()
        # M3.2: post-register wizard with caps, OFF switches, detect
        # + probe + save, and the explainer. Buttons resolve
        # statically (see test_id_lookups_resolve_statically).
        self.assertHas(src, "function openOnboarding")
        self.assertHas(src, "새 저장소 안내")
        self.assertHas(src, "/api/onboard/caps")
        self.assertHas(src, "/api/repo/detect")
        self.assertHas(src, 'id="ob-probe"')
        self.assertHas(src, 'id="ob-save"')
        self.assertHas(src, 'id="ob-close"')
        self.assertHas(src, "스위치 전부 꺼짐")
        self.assertHas(src, "절대 자동이 아닙니다")

    def test_s18e_next_scan_reads_approximate(self):
        src = self.src()
        # S18e: the glance never states the drifting estimate as
        # exact — "약 HH:MM" only.
        self.assertHas(src, "function fmtKSTapprox")
        self.assertHas(src, "fmtKSTapprox(sys.next_scan)")

    def test_mobile_sidebar_overlay_dismissal(self):
        src = self.src()
        # overlay width is clamped inside the viewport (persisted desktop
        # --side-w untouched, no localStorage reset); dismissal paths are
        # a visible close button, a backdrop tap, and Escape.
        self.assertHas(src, "calc(100vw - 56px)")
        self.assertHas(src, 'id="sideclose"')
        self.assertHas(src, 'id="sidebackdrop"')
        self.assertHas(src, "function openSide(")
        self.assertHas(src, "function closeSide(")
        self.assertHas(src, "$(\"sidebackdrop\").onclick")
        self.assertHas(src, "$(\"sideclose\").onclick")
        # nav/repo/chat selection still dismisses (now via closeSide so
        # the backdrop hides with the panel).
        self.assertHas(src, "closeSide(false)")
        # Escape closes only the mobile overlay, never the modal/palette.
        seg = src[src.index("function closeSide("):]
        seg = seg[:seg.index("async function loadAll")]
        self.assertHas(seg, 'e.key !== "Escape"')
        self.assertHas(seg, "max-width: 820px")
        self.assertHas(seg, "closeSide(true)")
        # desktop width persistence is intact: resize persistence stays,
        # and no code path clears the stored width.
        self.assertHas(src, 'localStorage.setItem("om_side_w"')
        self.assertLacks(src, 'removeItem("om_side_w"')
        self.assertLacks(src, 'setItem("om_side_w", "248')

    def test_home_has_single_decision_section(self):
        # 286: cap label removed; the heading itself says what the user does.
        src = self.src()
        self.assertIn("<h2>답변이 필요한 일</h2>", src)
        self.assertNotIn('<span class="cap">결정 필요</span>', src)
        self.assertNotIn("지금 해야 할 일", src)
        self.assertNotIn('<span class="cap">끝낸 일</span>', src)

    def test_refs_hydration_cached_per_view(self):
        src = self.src()
        # official metadata fills title-first rows; view-open fetch only
        self.assertHas(src, "let refCache = {};")
        self.assertHas(src, "async function fetchRefs")
        self.assertHas(src, "async function hydrateRefs")
        self.assertHas(src, "/api/refs?repo=")
        self.assertHas(src, "hydrateRefs($(\"msgs\")")
        # the 30s poll loop never refetches: cache is the authority
        seg = src[src.index("async function loadAll"):]
        seg = seg[:seg.index("async function pollJob")]
        self.assertLacks(seg, "hydrateRefs")
        self.assertLacks(seg, "fetchRefs")
        # states come from the ref object itself, never asserted blind
        self.assertHas(src, "REF_STATE_KO[r.state]")
        self.assertHas(src, "제목 미확인(이슈 링크에서 직접 확인)")
        # typed PR links point at the official discussion/files URLs
        self.assertHas(src, "PR 변경 파일 ↗")
        self.assertHas(src, "PR 논의 ↗")
        self.assertHas(src, '"/files"')
        # stale-after-failure metadata stays labeled, never silent
        self.assertHas(src, "캐시(갱신 실패)")

    def test_live_run_label_title_first(self):
        src = self.src()
        # shared helper: actual run title primary, Korean stage,
        # repo/tech ids secondary in tooltip only
        self.assertHas(src, "function liveRunHead")
        self.assertHas(src, 'triage: "분류"')
        self.assertHas(src, 'analyze: "분석"')
        self.assertHas(src, 'fix: "수정"')
        seg = src[src.index("function paintStatusbar"):]
        seg = seg[:seg.index("function render()")]
        self.assertHas(seg, "liveRunHead(live)")
        self.assertLacks(seg, "<b>${esc(live.repo)}#${esc(live.issue)}</b>")
        # busy indicator, elapsed ticker, and cancel survive the relabel
        self.assertHas(seg, 'class="pulse"')
        self.assertHas(seg, 'data-since="${lh.started}"')
        self.assertHas(seg, "data-jcancel")
        self.assertHas(seg, 'id="sb-view"')
        # quota/compat path uses the same helper (never a second copy of the
        # label logic), exact run-id navigation kept
        self.assertHas(src, "liveRunHead(quotaRun)")
        self.assertHas(src, "selRun = liveRunId")
        # work header leads with the human title, repo#issue secondary
        self.assertHas(src, '<h1 title="${esc(s.repo)}#${esc(s.issue)}')
        self.assertLacks(src, "<h1>${esc(s.repo)}#${esc(s.issue)}")

    def test_refs_single_badge_per_row(self):
        src = self.src()
        seg = src[src.index("async function hydrateRefs"):]
        seg = seg[:seg.index("async function openDraft")]
        # one semantic kind/state badge per row; the note slot is removed
        # once the badge exists, so re-runs cannot stack a second one
        self.assertHas(seg, "note.remove()")
        self.assertHas(seg, "data-refmeta")
        self.assertLacks(seg, "note.textContent = `${kindKo}")
        card = src[src.index("function summaryCard"):]
        card = card[:card.index("async function openDraft")]
        # no stale '이슈' prefix on unverified placeholders; verified PR
        # rows get their kind from official metadata, never asserted blind
        self.assertLacks(card, "이슈 #${num}</b>")
        self.assertHas(card, "표기 기준")
        self.assertHas(card, 'class="provnote"')
        # typed PR links and honest failure text stay
        self.assertHas(card + seg, "PR 변경 파일 ↗")
        self.assertHas(card + seg, "PR 논의 ↗")
        self.assertHas(seg, "제목 미확인(이슈 링크에서 직접 확인)")

    def test_source_changes_inspector(self):
        src = self.src()
        self.assertHas(src, "function paintChangesSection")
        self.assertHas(src, "function openDiffModal")
        self.assertHas(src, "sourceChanges")
        self.assertHas(src, "/api/work/changes?run=")
        self.assertHas(src, "chg-refresh")
        self.assertHas(src, "실행 전이라 변경 내용이 없습니다")
        self.assertLacks(src, "가짜 diff")
        self.assertHas(src, "미추적")
        self.assertHas(src, "원격 커밋 링크 없음")
        self.assertLacks(src, "github.com/commit")
        # completion is proof-gated, never blanket local-verified
        self.assertHas(src, "완료(과거 기록 · 현재 검증 없음)")
        self.assertHas(src, "완료(검증 기록 없음)")
        self.assertLacks(src, "완료(로컬 검증)")
        # core6 sections: availability states, live HEAD, patch provenance
        self.assertHas(src, "HISTORICAL")
        self.assertHas(src, "실제HEAD")
        self.assertHas(src, "기록된 커밋과 실제 HEAD가 다름")
        self.assertHas(src, "미커밋")
        self.assertHas(src, "무시·잡음")
        self.assertHas(src, "줄임 경로")
        self.assertHas(src, "스테이징됨")
        self.assertHas(src, "미스테이징")
        self.assertHas(src, "처리 경위")

    def test_work_review_action_and_human_status_text(self):
        src = self.src()
        # completed runs open this run's own review proof in place;
        # the command-center loop is gone
        self.assertHas(src, "function reviewNextFor")
        self.assertHas(src, "review-open")
        self.assertHas(src, "review-scroll")
        self.assertHas(src, "review-empty")
        self.assertHas(src, "결과 검토")
        self.assertHas(src, "이 실행의 리뷰 증거를 대화상자로 엽니다")
        self.assertHas(src, "저장된 검토 증거가 없습니다")
        self.assertLacks(src, "관제센터에서 결과를 검토합니다")
        # promotion blockers read as concise Korean; raw English
        # criteria live only inside the collapsed original-text details
        self.assertHas(src, "function blockerKo")
        self.assertHas(src, "현재 소스 기준 리뷰 증거 없음")
        self.assertHas(src, "현재 소스 기준 검사 증거 없음")
        self.assertHas(src, "승격 조건 원문 보기")
        # e2e step label is neutral; device wording stays only on
        # real command/scope-derived verdict lines
        self.assertHas(src, 'e2e: "검증"')
        self.assertLacks(src, 'e2e: "기기 검증"')
        self.assertHas(src, "기기·네이티브 실행 확인됨")

    def test_home_events_compact_and_single_answer(self):
        src = self.src()
        # giant scan dumps collapse; package names link the real deps tab
        self.assertHas(src, "function eventHtml")
        self.assertHas(src, "function eventDetailHtml")
        self.assertHas(src, "상세 보기")
        self.assertHas(src, "?tab=deps")
        # one semantic answer area inside the decision card
        self.assertLacks(src, "blocked-card")
        self.assertLacks(src, "paintBlocked")
        self.assertLacks(src, "답변창으로")
        self.assertHas(src, 'data-answer="${r.id}"')

    def test_registry_links_per_manager(self):
        src = self.src()
        self.assertHas(src, "function registryUrl")
        self.assertHas(src, "pub.dev/packages/")
        self.assertHas(src, "pub.dev ↗")
        self.assertHas(src, "npm 레지스트리 ↗")
        # no always-npm URL builder anymore; unknown managers get no link
        self.assertLacks(src, 'const npmUrl = "https://www.npmjs.com/package/"')
        self.assertHas(src, "registryUrl((d0 && d0.kind) || \"\", dep)")

    def test_graph_inspector_title_first(self):
        src = self.src()
        self.assertHas(src, "function runNodeTitle")
        self.assertHas(src, "function branchIsActual")
        self.assertHas(src, "function branchStateOf")
        self.assertHas(src, "gd-title")
        self.assertHas(src, "과거 기록 · 현재 검증 없음")
        self.assertHas(src, "원문 근거 보기")
        self.assertHas(src, "실제 작업 브랜치")
        self.assertHas(src, "제안된 목표 브랜치(예정)")
        self.assertHas(src, "브랜치 실체 확인 불가")
        # core7 optional node fields consumed with fallback, never forged
        self.assertHas(src, "object_kind")
        self.assertHas(src, "n._brState")
        self.assertLacks(src, "원본 글자 크기로 보여줍니다")

    def test_home_title_first_and_translated_summary(self):
        src = self.src()
        # human purpose title primary, synthetic dep IDs tooltip-only
        self.assertHas(src, "function homeRunHead")
        self.assertHas(src, "homeRunHead(r)")
        self.assertHas(src, "hh.primary")
        self.assertHas(src, "hh.tip")
        self.assertHas(src, "의존성 업데이트")
        # raw last_outcome English never renders untranslated
        self.assertHas(src, "STATUS_KO[rawRes] || GST_KO[rawRes]")

    def test_dep_exec_gated_by_queue_status(self):
        src = self.src()
        seg = src[src.index("async function paintRepoDeps"):]
        seg = seg[:seg.index("async function paintRepoBranches")]
        # only queued exact keys get an active execute button; other
        # statuses get the actual reason plus the linked run
        self.assertHas(src, "function depActionFor")
        self.assertHas(seg, "depActionFor(it)")
        self.assertHas(seg, "act.exec")
        self.assertHas(seg, "연결된 실행 열기")
        self.assertHas(src, "리뷰 대기 중 — 지금은 실행할 수 없습니다(중복 실행 방지)")
        # legitimate queued execution path is preserved, backend authoritative
        self.assertHas(seg, "이 제안 실행")
        self.assertHas(seg, 'jpost("/api/sched/run-due", {repo, key, fake: false})')

    def test_graph_inspector_full_titles_typed_links(self):
        src = self.src()
        # group members show full wrapped titles, number/kind secondary
        self.assertHas(src, "function aggMemberHead")
        self.assertHas(src, "aggMemberHead(m)")
        self.assertHas(src, "mh.primary")
        # raw provenance folded; relation names human-readable
        self.assertHas(src, "출처 보기")
        self.assertHas(src, "근거 보기")
        self.assertHas(src, 'mentions: "언급"')
        # confirmed PRs get typed discussion/files links, issues get Issue-only
        self.assertHas(src, "PR 논의 ↗")
        self.assertHas(src, "PR 변경 파일 ↗")
        self.assertHas(src, "isPrNode")

    def test_no_code_outcome_safe_fallback(self):
        src = self.src()
        # explicit no-code truth never renders as a verified upgrade
        self.assertHas(src, "function isNoCodeDone")
        self.assertHas(src, "noCodeSettled")
        self.assertHas(src, "완료(코드 변경 없음 · 중복)")
        self.assertHas(src, "코드 변경 없음(중복)")
        # stale verification + live-empty + exact settlement = validated no-change (§10, UI13)
        self.assertHas(src, "function isValidatedNoChange")
        self.assertHas(src, "function scAvailOf")
        self.assertHas(src, "function settledNextFor")
        self.assertHas(src, "완료(변경 불필요 · 이미 반영됨)")
        # live empty diff is current no-change even with a legacy planned flag;
        # true planned stays pre-run, unreadable stays unknown (never empty)
        self.assertHas(src, "현재 소스 변경 없음")
        self.assertHas(src, "실행 전이라 변경 내용이 없습니다")
        self.assertHas(src, "확인 불가")
        # settled summary leads with the current result; past evidence stays folded
        self.assertHas(src, "이미 반영되어 소스 변경이 필요하지 않았습니다")
        self.assertHas(src, "판정 근거/과거 기록")
        # settled next action always opens the rendered record, never a guessed file
        self.assertHas(src, "처리 기록 보기")
        self.assertHas(src, "settled-record")
        self.assertHas(src, "변경 불필요 판정의 근거를 펼칩니다")
        self.assertLacks(src, "과거 검증 기록 보기")
        # cancelled-superseded renders distinctly with the kept run (§8)
        self.assertHas(src, "대체됨(실행 없음)")
        self.assertHas(src, "실행 기록 열기")
        # ignored-lock-only rows show their honest reason (§9)
        self.assertHas(src, 'd.update_kind === "none"')

    def test_answer_queued_state_truthful(self):
        src = self.src()
        # one shared helper owns the §14 answer states; all three answer
        # paths (command center, run/chat, work detail) route through it
        for token in ("function answerOutcome", "function answerErrorIsSaved",
                      "function handleAnswerOk", "function handleAnswerError"):
            self.assertHas(src, token)
        self.assertEqual(src.count("await handleAnswerOk(r);"), 3,
                         "all three answer paths share handleAnswerOk")
        self.assertHas(src, "handleAnswerError(e, btn)")
        self.assertHas(src, "handleAnswerError(e, ab)")
        self.assertHas(src, 'handleAnswerError(e, $("w-send"))')
        # queued answers say queued; unqueued distinguishes saved from
        # not-yet-reserved; only started:true keeps the execution copy
        self.assertHas(src, "답변 저장됨. 워커가 비면 같은 작업으로 이어집니다")
        self.assertHas(src, "아직 자동 재개가 예약되지 않았습니다")
        self.assertHas(src, "answer saved")
        # the old unconditional claim-then-poll is gone from every path
        self.assertLacks(src, '이어서 실행합니다."); pollJob')


class OverviewOperationalStatusTests(ServeMixin):
    """Operational-status honesty: liveness probe, scan freshness, counts.

    Simple actual queue + owned-watch run shapes (slider review, babel
    watch, cancelled intl with historical watch fields) — no UI fixture
    duplication that ignores the backend.
    """

    def _row(self):
        code, body = self.get("/api/overview")
        self.assertEqual(code, 200)
        rows = {r["name"]: r for r in body["repos"]}
        return body, rows["fakerepo"]

    def _save_queue(self, queue, **fields):
        from datetime import datetime, timezone
        st = sched.load("fakerepo")
        st["queue"] = queue
        st["enabled"] = fields.get("enabled", True)
        st["execute_enabled"] = True
        st["interval_s"] = 1800
        st["current_job"] = fields.get("current_job")
        now_iso = datetime.now(timezone.utc).isoformat()
        st["last_scan"] = fields.get("last_scan", now_iso)
        st["next_scan"] = fields.get("next_scan", now_iso)
        st["scan_ok"] = fields.get("scan_ok", True)
        st["error"] = None
        sched.save(st)
        return st

    def test_idle_lock_reports_not_live(self):
        offline_service_status(self, {"installed": True, "running": False, "pid": None, "note": "fixture offline; scheduler discovery is not under test"})  # noqa: E501
        self._save_queue([])
        body, _ = self._row()
        lock = body["worker_lock"]
        self.assertIsNotNone(lock)
        self.assertFalse(lock["live"])
        self.assertIsNone(lock["owner"])

    def test_stale_dead_metadata_never_reports_running(self):
        offline_service_status(self, {"installed": True, "running": False, "pid": None, "note": "fixture offline; scheduler discovery is not under test"})  # noqa: E501
        dead = 1 << 30
        self.assertFalse(sched._pid_alive(dead))
        meta = {"owner": f"pid-{dead}", "pid": dead,
                "at": time.time() - 10000,
                "heartbeat": time.time() - 10000}
        sched._lock_file().parent.mkdir(parents=True, exist_ok=True)
        sched._lock_file().write_text(json.dumps(meta), encoding="utf-8")
        before = sched._lock_file().read_bytes()
        self._save_queue([])
        body, _ = self._row()
        lock = body["worker_lock"]
        self.assertFalse(lock["live"],
                         "dead-PID metadata with a free lease must not read live")
        self.assertEqual(lock["owner"], f"pid-{dead}",
                         "historical owner stays as inactive evidence only")
        self.assertEqual(sched._lock_file().read_bytes(), before,
                         "overview must not touch the lease")
        fd = sched._flock_try_hold()
        self.assertIsNotNone(fd, "lease must still be free after overview")
        import fcntl
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def test_live_holder_reports_live(self):
        # Service discovery row is scheduler input; the live lease under
        # test stays real via acquire_worker_lease.
        offline_service_status(self, {"installed": True, "running": False, "pid": None, "note": "fixture offline; scheduler discovery is not under test"})  # noqa: E501
        owner = sched.acquire_worker_lease(owner="test-holder")
        self.assertIsNotNone(owner)
        self.addCleanup(sched.release_worker_lease, owner)
        self._save_queue([])
        body, _ = self._row()
        lock = body["worker_lock"]
        self.assertTrue(lock["live"])
        self.assertEqual(lock["owner"], "test-holder")

    def test_live_flock_without_metadata_is_busy_unknown(self):
        # Lock fixture is worker-lease evidence; service row is scheduler
        # input and stays an explicit owned fixture row.
        offline_service_status(self, {"installed": True, "running": False, "pid": None, "note": "fixture offline; scheduler discovery is not under test"})  # noqa: E501
        import fcntl
        fd = sched._flock_try_hold()
        self.assertIsNotNone(fd)

        def _release():
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
        self.addCleanup(_release)
        try:
            sched._lock_file().unlink()
        except OSError:
            pass
        self._save_queue([])
        body, _ = self._row()
        lock = body["worker_lock"]
        self.assertTrue(lock["live"],
                        "live flock with missing metadata is busy, unknown identity")
        self.assertIsNone(lock["owner"])

    def test_watch_excluded_from_actionable_review_counts(self):
        offline_service_status(self, {"installed": True, "running": False, "pid": None, "note": "fixture offline; scheduler discovery is not under test"})  # noqa: E501
        queue = [
            {"key": "k-slider", "kind": "dep-update", "status": "waiting-review",
             "run_id": "run-slider", "attempts": 0},
            {"key": "k-babel", "kind": "dep-update", "status": "waiting-review",
             "run_id": "run-babel", "attempts": 2,
             "note": "external-watch: peer toolchain incompatible",
             "watch": {"reason": "peer toolchain incompatible",
                       "trigger": "compatible peer release",
                       "next_check": "2026-10-17T22:47:34+00:00",
                       "active": True}},
            {"key": "k-intl", "kind": "dep-update", "status": "cancelled",
             "run_id": "run-intl", "attempts": 0,
             "note": "superseded: already reflected; no source change",
             "watch": {"reason": "old", "active": True}},
            {"key": "k-other", "kind": "dep-update", "status": "queued",
             "attempts": 0},
        ]
        self._save_queue(queue)
        _, row = self._row()
        self.assertEqual(row["queue_counts"],
                         {"queued": 1, "running": 0, "blocked": 0,
                          "waiting-review": 1, "deferred": 1})
        self.assertEqual(row["next_action"], "리뷰 대기 1건 확인")
        raw = sched.load("fakerepo")
        by_key = {i["key"]: i["status"] for i in raw["queue"]}
        self.assertEqual(by_key["k-babel"], "waiting-review",
                         "raw queue status stays backward compatible")
        self.assertEqual(by_key["k-intl"], "cancelled")

    def test_watch_only_repo_has_no_review_action(self):
        offline_service_status(self, {"installed": True, "running": False, "pid": None, "note": "fixture offline; scheduler discovery is not under test"})  # noqa: E501
        queue = [
            {"key": "k-babel", "kind": "dep-update", "status": "waiting-review",
             "run_id": "run-babel", "attempts": 2,
             "note": "external-watch: peer toolchain incompatible",
             "watch": {"reason": "peer toolchain incompatible",
                       "trigger": "compatible peer release",
                       "next_check": "2026-10-17T22:47:34+00:00",
                       "active": True}},
        ]
        self._save_queue(queue)
        _, row = self._row()
        self.assertEqual(row["queue_counts"]["waiting-review"], 0)
        self.assertEqual(row["queue_counts"]["deferred"], 1)
        self.assertNotIn("리뷰", row["next_action"])

    def test_scan_freshness_delay_and_recovery(self):
        from datetime import datetime, timedelta, timezone
        now = time.time()
        iso = lambda ts: datetime.fromtimestamp(
            ts, tz=timezone.utc).isoformat()
        overdue = {"enabled": True,
                   "last_scan": iso(now - 7200),
                   "next_scan": iso(now - 3600)}
        idle = serve._scan_freshness(overdue, now_ts=now, worker_busy=False)
        self.assertEqual(idle["state"], "delayed-check")
        self.assertIn("서비스 확인 필요", idle["detail"])
        busy = serve._scan_freshness(overdue, now_ts=now, worker_busy=True)
        self.assertEqual(busy["state"], "delayed-processing")
        self.assertIn("처리 중", busy["detail"])
        fresh = serve._scan_freshness(
            {"enabled": True, "last_scan": iso(now - 60),
             "next_scan": iso(now - 3600)},
            now_ts=now, worker_busy=False)
        self.assertEqual(fresh["state"], "fresh")
        off = serve._scan_freshness(
            {"enabled": False, "last_scan": iso(now - 7200),
             "next_scan": iso(now - 3600)},
            now_ts=now, worker_busy=False)
        self.assertEqual(off["state"], "off")
        unknown = serve._scan_freshness(
            {"enabled": True, "last_scan": None, "next_scan": None},
            now_ts=now, worker_busy=False)
        self.assertEqual(unknown["state"], "unknown")
        ok = serve._scan_freshness(
            {"enabled": True, "last_scan": iso(now - 7200),
             "next_scan": iso(now + 600)},
            now_ts=now, worker_busy=False)
        self.assertEqual(ok["state"], "ok")

    def test_scan_ok_kept_separate_from_freshness(self):
        offline_service_status(self, {"installed": True, "running": False, "pid": None, "note": "fixture offline; scheduler discovery is not under test"})  # noqa: E501
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        self._save_queue(
            [], last_scan=(now - timedelta(hours=2)).isoformat(),
            next_scan=(now - timedelta(hours=1)).isoformat(),
            scan_ok=True)
        _, row = self._row()
        self.assertTrue(row["scan_ok"])
        self.assertEqual(row["scan_freshness"]["state"], "delayed-check")


class JobResumePreflightTests(unittest.TestCase):
    """api_job_resume refuses gated runs read-only: truthful 409, no job.

    A late post-triage question (ACTIONABLE disposition + current
    question text) must never create a resume thread. The refusal saves
    nothing, so a concurrent api_job_answer write can never be
    clobbered by this snapshot.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_temp_home(self, str(Path(self.tmp.name) / "home"))
        # Same host-PATH independence as ServeMixin: the gate probes
        # muse.which(), which must not depend on a real host binary.
        offline_muse_binary(self, "/fixture/muse")
        repos.save(dict(PROFILE))
        serve._JOBS.clear()
        serve._JOB_CANCEL.clear()

    def _question_run(self):
        state = store.create("fakerepo", "7", "t", "base0", {})
        runner.set_disposition(state, "ACTIONABLE", "fixture actionable")
        state = store.load(state["id"])
        state["waitReason"] = ("question: Should the coupled tool also be "
                               "bumped in lockstep, or only the target?")
        state["question_step"] = "analyze"
        state["steps"]["triage"] = {"status": "done", "exit_code": 0}
        state["steps"]["analyze"] = {"status": "blocked", "exit_code": 0,
                                     "note": "waiting on operator"}
        store.save(state)
        return state["id"]

    def _run_bytes(self, run_id):
        return (store.run_dir(run_id) / "run.json").read_bytes()

    def test_waiting_question_refuses_without_job_or_mutation(self):
        run_id = self._question_run()
        before = self._run_bytes(run_id)
        calls = []
        with mock.patch.object(serve, "_lease_probe",
                               side_effect=lambda: calls.append("lease") or True), \
             mock.patch.object(serve, "_job_new",
                               side_effect=lambda *a: calls.append("new")), \
             mock.patch.object(serve, "_job_run",
                               side_effect=lambda *a: calls.append("run")):
            with self.assertRaises(serve.ApiError) as ctx:
                serve.api_job_resume({"run": run_id})
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("waits for operator", ctx.exception.message)
        self.assertIn("lockstep", ctx.exception.message)
        self.assertEqual(calls, [])
        self.assertEqual(serve._JOBS, {})
        self.assertEqual(self._run_bytes(run_id), before)
        self.assertTrue(store.load(run_id)["waitReason"].startswith(
            "question:"))

    def test_stale_refusal_never_clobbers_recorded_answer(self):
        import copy
        run_id = self._question_run()
        stale = copy.deepcopy(store.load(run_id))
        self.assertTrue(stale["waitReason"].startswith("question:"))
        store.record_answer(run_id, "Bump in lockstep per repo policy.")
        answered_bytes = self._run_bytes(run_id)
        calls = []
        with mock.patch.object(store, "load",
                               return_value=copy.deepcopy(stale)), \
             mock.patch.object(serve, "_job_new",
                               side_effect=lambda *a: calls.append("new")):
            with self.assertRaises(serve.ApiError) as ctx:
                serve.api_job_resume({"run": run_id})
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(calls, [])
        # The official answer survives the stale refusal untouched.
        live = store.load(run_id)
        self.assertIsNone(live.get("waitReason"))
        self.assertEqual(live["answers"][-1]["a"],
                         "Bump in lockstep per repo policy.")
        self.assertEqual(self._run_bytes(run_id), answered_bytes)


class ProfileHttpTests(ServeMixin):
    def test_profile_get_post_roundtrip(self):
        import sys as _sys
        tools = Path(self.tmp.name) / "tools"
        tools.mkdir()
        (tools / "probe-tool").symlink_to(_sys.executable)
        code, body = self.get("/api/repo/profile?repo=fakerepo")
        self.assertEqual(code, 200)
        self.assertEqual(body["repo"], "fakerepo")
        self.assertIn("validation", body)
        # P1: GET never probes.
        self.assertEqual(body["probes"], [])
        code, body = self.post("/api/repo/profile",
                               {"repo": "fakerepo", "probe": True})
        self.assertEqual(code, 200)
        # No steps configured → no probes; the key still answers.
        self.assertEqual(body["probes"], [])
        fc = {"steps": [{"argv": ["probe-tool"], "gate": "unit"}],
              "toolchain": [str(tools)]}
        code, body = self.post("/api/repo/profile",
                               {"repo": "fakerepo", "fastcheck": fc})
        self.assertEqual(code, 400)
        self.assertIn("confirm", body.get("error", ""))
        code, body = self.post("/api/repo/profile",
                               {"repo": "fakerepo", "fastcheck": fc,
                                "confirm": True})
        self.assertEqual(code, 200)
        self.assertEqual(body["validation"], [])
        self.assertTrue(body["probes"][0]["ok"])
        self.assertEqual(
            repos.load("fakerepo")["fastcheck"]["steps"][0]["argv"],
            ["probe-tool"])

    def test_profile_card_renders_from_api(self):
        src = (Path(__file__).resolve().parent.parent / "src" /
               "openmainty" / "dashboard.html").read_text(
                   encoding="utf-8")
        self.assertIn('id="profcard"', src)
        self.assertIn("/api/repo/profile?repo=", src)
        self.assertIn("profcfg", src)
        self.assertIn("confirm(", src)
        # P1/P2: explicit probe button + honest fetch wording.
        self.assertIn("도구 확인", src)
        self.assertIn("내려받기", src)

    def test_profile_post_evil_origin_refused(self):
        req = urllib.request.Request(
            self.url("/api/repo/profile"),
            data=json.dumps({"repo": "fakerepo"}).encode(),
            headers={"Content-Type": "application/json",
                     "Origin": "https://evil.example"},
            method="POST")
        try:
            urllib.request.urlopen(req, timeout=30)
            self.fail("evil origin must be refused")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 403)


class AttestHttpTests(ServeMixin):
    """M2.4b: the attest endpoint needs confirm and surfaces the
    validator's refusal."""

    def test_attest_needs_confirm(self):
        code, body = self.post("/api/batch/attest",
                               {"batch": "b", "gate": "unit",
                                "attestor": "owner", "note": "x",
                                "mode": "receipt"})
        self.assertEqual(code, 400)

    def test_attest_unknown_batch_409(self):
        code, body = self.post("/api/batch/attest",
                               {"batch": "no-such-batch",
                                "gate": "unit", "attestor": "owner",
                                "note": "x", "mode": "receipt",
                                "receipt_sha": "0" * 64,
                                "confirm": True})
        self.assertEqual(code, 409)


class BatchReviewApiTests(ServeMixin):
    """M2.16c: POST /api/batch/review {batch, run, verdict, by,
    findings, confirm} — one human verdict on one batch primary.

    Worker/missing-confirm refuse with 400 before anything runs;
    unknown runs refuse with 409. The happy path records through
    the real batches.set_review and queues (never runs) the
    background refresh.
    """

    def _write_batch(self, bid="api-rv-1", run_id="run-rv-1"):
        from openmainty import batches as _batches
        from openmainty.util import write_json as _write_json
        wt = Path(self.tmp.name) / "wt"
        wt.mkdir()
        self._git(wt, "init", "-b", "main")
        (wt / "f.txt").write_text("v1\n")
        self._git(wt, "add", ".")
        self._git(wt, "commit", "-m", "base")
        head = self._git_head(wt)
        from openmainty import runner as _runner
        fp = _runner.source_snapshot(wt)["fingerprint"]
        _write_json(_batches.batch_file(bid), {
            "id": bid, "repo": "fakerepo", "target_branch": "main",
            "origin": {"checkout": str(wt), "base_branch": "main",
                       "base_head": head},
            "primaries": [{"dep": "jest", "target": "30.5.2",
                           "lane": "next", "run_id": run_id,
                           "source": {"worktree": str(wt),
                                      "branch": "main", "head": head,
                                      "fingerprint": fp},
                           "review": {"verdict": "pending"}}],
            "integrations": [], "required_gates": []})
        return bid, run_id

    def _git(self, cwd, *args):
        import subprocess
        subprocess.run(["git", "-c", "user.email=t@t",
                        "-c", "user.name=t", *args], cwd=str(cwd),
                       check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)

    def _git_head(self, cwd):
        import subprocess
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(cwd), check=True,
            text=True, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL).stdout.strip()

    def test_worker_400(self):
        code, body = self.post("/api/batch/review",
                               {"batch": "b", "run": "r",
                                "verdict": "accept", "by": "worker",
                                "findings": [], "confirm": True})
        self.assertEqual(code, 400)

    def test_missing_confirm_400(self):
        code, _ = self.post("/api/batch/review",
                            {"batch": "b", "run": "r",
                             "verdict": "accept", "by": "root",
                             "findings": []})
        self.assertEqual(code, 400)

    def test_unknown_run_409(self):
        self._write_batch()
        code, body = self.post(
            "/api/batch/review",
            {"batch": "api-rv-1", "run": "run-nope-9",
             "verdict": "accept", "by": "root", "findings": [],
             "confirm": True})
        self.assertEqual(code, 409)

    def test_unknown_verdict_400(self):
        code, _ = self.post(
            "/api/batch/review",
            {"batch": "b", "run": "r", "verdict": "bogus",
             "by": "root", "findings": [], "confirm": True})
        self.assertEqual(code, 400)

    def test_notes_without_findings_400(self):
        self._write_batch()
        code, _ = self.post(
            "/api/batch/review",
            {"batch": "api-rv-1", "run": "run-rv-1",
             "verdict": "accept-with-notes", "by": "root",
             "findings": [], "confirm": True})
        self.assertEqual(code, 400)

    def test_long_finding_400(self):
        self._write_batch()
        code, _ = self.post(
            "/api/batch/review",
            {"batch": "api-rv-1", "run": "run-rv-1",
             "verdict": "accept-with-notes", "by": "root",
             "findings": ["x" * 201], "confirm": True})
        self.assertEqual(code, 400)

    def test_too_many_findings_400(self):
        self._write_batch()
        code, _ = self.post(
            "/api/batch/review",
            {"batch": "api-rv-1", "run": "run-rv-1",
             "verdict": "request-changes", "by": "root",
             "findings": ["f%d" % i for i in range(21)],
             "confirm": True})
        self.assertEqual(code, 400)

    def test_happy_path_queues_background_refresh(self):
        from unittest import mock as _mock
        self._write_batch()
        with _mock.patch.object(
                serve.batches, "refresh_all_snapshots",
                side_effect=AssertionError("inline")):
            code, body = self.post(
                "/api/batch/review",
                {"batch": "api-rv-1", "run": "run-rv-1",
                 "verdict": "request-changes", "by": "owner",
                 "findings": ["다시 확인"], "confirm": True})
        self.assertEqual(code, 200)
        self.assertEqual(body.get("verdict"), "request-changes")
        from openmainty import batches as _batches
        got = _batches.load_batch("api-rv-1")["primaries"][0][
            "review"]
        self.assertEqual(got["verdict"], "request-changes")
        self.assertEqual(got["previous"]["verdict"], "pending")

    def test_attest_endpoint_passes_no_refresh(self):
        from unittest import mock as _mock
        seen = {}
        real = serve.batches.attest_gate

        def fake(batch_id, gate_key, attestor, note, mode,
                 **kw):
            seen.update(kw)
            return {"batch": batch_id, "gate": gate_key,
                    "attestor": attestor, "mode": mode}

        serve.batches.attest_gate = fake
        try:
            with _mock.patch.object(
                    serve.batches, "refresh_all_snapshots",
                    side_effect=AssertionError("inline")):
                code, _ = self.post(
                    "/api/batch/attest",
                    {"batch": "b", "gate": "unit",
                     "attestor": "owner", "note": "x",
                     "mode": "na", "na_reason": "해당 없음",
                     "confirm": True})
        finally:
            serve.batches.attest_gate = real
        self.assertEqual(code, 200)
        self.assertIs(seen.get("refresh"), False)

    def test_dashboard_posts_review_with_confirm(self):
        src = (Path(__file__).resolve().parent.parent / "src"
               / "openmainty" / "dashboard.html").read_text(
                   encoding="utf-8")
        for token in ("data-rv2-go",
                      "/api/batch/review",
                      "독립 검토:",
                      "메모와 함께 승인",
                      "data-rv2-find",
                      "소견 한 줄에 하나"):
            self.assertIn(token, src)

    def test_dashboard_m216d_tokens(self):
        src = (Path(__file__).resolve().parent.parent / "src"
               / "openmainty" / "dashboard.html").read_text(
                   encoding="utf-8")
        for token in ("compiler_notes",
                      "새 버전 준비됨:",
                      "다시 합치기 가능",
                      "확인 필요:"):
            self.assertIn(token, src)


class FastcheckRetryHttpTests(ServeMixin):
    """L2: the retry flag travels from the POST body to the runner."""

    def test_retry_flag_reaches_runner(self):
        seen = {}
        real = serve.batches.run_fastcheck_for_op

        def fake(op_id, origin="ui", retry=False, _runner=None):
            seen.update(op=op_id, origin=origin, retry=retry)
            return {"op": op_id, "ok": True, "summary": {}}

        serve.batches.run_fastcheck_for_op = fake
        try:
            code, _ = self.post("/api/integration/fastcheck",
                                {"op": "op-x", "confirm": True,
                                 "retry": True})
            self.assertEqual(code, 200)
            self.assertTrue(seen.get("retry"))
            code, _ = self.post("/api/integration/fastcheck",
                                {"op": "op-x", "confirm": True})
            self.assertEqual(code, 200)
            self.assertFalse(seen.get("retry"))
        finally:
            serve.batches.run_fastcheck_for_op = real


class FastcheckRerunApiTests(ServeMixin):
    """M2.16c: POST /api/integration/fastcheck-rerun {op, by,
    confirm} — one explicit human re-verification of a done
    prepare op whose receipt predates a new gate.

    Worker/missing-confirm/missing-op refuse with 400 before
    the lease; a busy lease refuses with 409 and starts nothing;
    unknown and non-done ops refuse with 409. The driver never
    calls this endpoint.
    """

    def test_worker_400_starts_nothing(self):
        code, body = self.post("/api/integration/fastcheck-rerun",
                               {"op": "op-x", "by": "worker",
                                "confirm": True})
        self.assertEqual(code, 400)
        self.assertIn("worker", body.get("error", ""))

    def test_missing_confirm_400(self):
        code, _ = self.post("/api/integration/fastcheck-rerun",
                            {"op": "op-x", "by": "root"})
        self.assertEqual(code, 400)

    def test_missing_op_400(self):
        code, _ = self.post("/api/integration/fastcheck-rerun",
                            {"by": "root", "confirm": True})
        self.assertEqual(code, 400)

    def test_busy_lease_409_starts_nothing(self):
        owner = sched.acquire_worker_lease(owner="test-holder")
        self.assertIsNotNone(owner)
        self.addCleanup(sched.release_worker_lease, owner)
        code, body = self.post("/api/integration/fastcheck-rerun",
                               {"op": "op-x", "by": "root",
                                "confirm": True})
        self.assertEqual(code, 409)
        self.assertIn("busy", body.get("error", ""))

    def test_unknown_op_409(self):
        code, body = self.post("/api/integration/fastcheck-rerun",
                               {"op": "op-nope-9", "by": "root",
                                "confirm": True})
        self.assertEqual(code, 409)
        self.assertIn("unknown", body.get("error", ""))

    def test_happy_path_200_and_event(self):
        from openmainty import batches as _batches
        from openmainty import events as _events
        from openmainty import store as _store
        from openmainty.util import write_json as _write_json
        _store.save({"id": "run-re1", "repo": "fakerepo", "dep": "foo",
                     "target": "1.2.3"})
        op_id = "prep-run-re1-batch-o"
        _write_json(_batches.op_file(op_id), {
            "op": "prepare", "op_id": op_id, "status": "done",
            "run_id": "run-re1", "dep": "foo", "target": "1.2.3",
            "result": {"path": str(Path(self.tmp.name)),
                       "branch": "b", "head": "h" * 40,
                       "fingerprint": "f" * 40}})

        def fake(op_id, by=""):
            _events.append("fakerepo", "m2-fastcheck-rerun", op_id,
                           f"검증 다시 실행 (by {by})",
                           f"{op_id}: 통과", actor=by)
            return {"op": op_id, "ok": True, "summary": {},
                    "archived_receipt": "x.prev.json",
                    "archived_logs": []}

        real = serve.batches.rerun_fastcheck_for_op
        serve.batches.rerun_fastcheck_for_op = fake
        try:
            code, body = self.post(
                "/api/integration/fastcheck-rerun",
                {"op": op_id, "by": "root", "confirm": True})
        finally:
            serve.batches.rerun_fastcheck_for_op = real
        self.assertEqual(code, 200)
        self.assertTrue(body.get("ok"))
        found = [e for e in _events.list_events("fakerepo")
                 if e.get("kind") == "m2-fastcheck-rerun"]
        self.assertTrue(found)
        self.assertIn("검증 다시 실행 (by root)",
                      found[-1].get("title") or "")

    def test_dashboard_posts_rerun_with_confirm(self):
        src = (Path(__file__).resolve().parent.parent / "src"
               / "openmainty" / "dashboard.html").read_text(
                   encoding="utf-8")
        for token in ("prep-fc-rerun-go",
                      "/api/integration/fastcheck-rerun",
                      "data-fc-rerun-by", "검증 다시 실행",
                      "지워지지 않습니다"):
            self.assertIn(token, src)


class HoldApiTests(ServeMixin):
    """M2.5b: POST /api/batch/hold (routed, gated, owner/root only)."""

    def _batch(self):
        import json as _json
        home = Path(os.environ["OPENMAINTY_HOME"])
        (home / "batches").mkdir(parents=True, exist_ok=True)
        (home / "batches" / "auto-hold-1.json").write_text(
            _json.dumps({"id": "auto-hold-1", "repo": "fakerepo",
                         "primaries": [], "integrations": []}),
            encoding="utf-8")

    def test_hold_round_trip(self):
        self._batch()
        code, _ = self.post("/api/batch/hold", {"batch": "auto-hold-1",
                                                "hold": True,
                                                "attestor": "owner"})
        self.assertEqual(code, 400)
        # M2.5c-N2: a missing hold key refuses instead of releasing.
        code, body = self.post("/api/batch/hold",
                               {"batch": "auto-hold-1",
                                "attestor": "root",
                                "reason": "x", "confirm": True})
        self.assertEqual(code, 400)
        self.assertIn("explicit boolean", body.get("error", ""))
        code, body = self.post("/api/batch/hold",
                               {"batch": "auto-hold-1", "hold": True,
                                "attestor": "worker",
                                "reason": "x", "confirm": True})
        self.assertEqual(code, 409)
        code, body = self.post("/api/batch/hold",
                               {"batch": "auto-hold-1", "hold": True,
                                "attestor": "root",
                                "reason": "metro lockstep",
                                "confirm": True})
        self.assertEqual(code, 200)
        self.assertTrue(body["hold"])
        code, body = self.post("/api/batch/hold",
                               {"batch": "auto-hold-1", "hold": False,
                                "attestor": "root", "confirm": True})
        self.assertEqual(code, 200)
        self.assertFalse(body["hold"])


class PolicyApiTests(ServeMixin):
    """M2.5d: POST /api/m2/policy `by` attribution."""

    def test_policy_flip_by(self):
        from openmainty import events
        # Default stays owner.
        code, _ = self.post("/api/m2/policy",
                            {"repo": "fakerepo", "stage": "prepare",
                             "on": True})
        self.assertEqual(code, 200)
        # Worker is refused and flips nothing.
        code, body = self.post("/api/m2/policy",
                               {"repo": "fakerepo", "stage": "prepare",
                                "on": True, "by": "worker"})
        self.assertEqual(code, 400)
        self.assertIn("owner", body.get("error", ""))
        # Root is attributed.
        code, _ = self.post("/api/m2/policy",
                            {"repo": "fakerepo", "stage": "prepare",
                             "on": False, "by": "root"})
        self.assertEqual(code, 200)
        actors = [e.get("actor")
                  for e in events.list_events("fakerepo")]
        self.assertIn("owner", actors)
        self.assertIn("root", actors)
        self.assertNotIn("worker", actors)


class FamilyRetryApiTests(unittest.TestCase):
    """M2.6b: POST /api/integration/family-retry validation order.

    Missing confirm refuses with 400 before touching the lease;
    a worker identity refuses with 403 and writes no op record.
    The happy path and train rules live in tests/test_m2_family.py
    (batches.family_retry); the driver never calls this endpoint.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_temp_home(self, str(Path(self.tmp.name) / "home"))
        # api_integration_family_retry gates on muse.which() first;
        # keep this class independent of host PATH like ServeMixin.
        offline_muse_binary(self, "/fixture/muse")

    def test_missing_confirm_400_starts_nothing(self):
        with self.assertRaises(serve.ApiError) as ctx:
            serve.api_integration_family_retry(
                {"repo": "r", "run_id": "x", "by": "owner"})
        self.assertEqual(ctx.exception.status, 400)

    def test_worker_403_without_writes(self):
        with self.assertRaises(serve.ApiError) as ctx:
            serve.api_integration_family_retry(
                {"repo": "r", "run_id": "x", "by": "worker",
                 "confirm": True})
        self.assertEqual(ctx.exception.status, 403)
        self.assertFalse(
            list((Path(os.environ["OPENMAINTY_HOME"]
                       ).glob("integration_ops/*.json"))))

    def test_dashboard_posts_family_retry_with_confirm(self):
        src = (Path(__file__).resolve().parent.parent / "src"
               / "openmainty" / "dashboard.html").read_text(
                   encoding="utf-8")
        for token in ("prep-family-go", "/api/integration/family-retry",
                      "가족으로 다시 시도", "data-fam-by",
                      "대체됨 — 가족", "출시 열차 전체를 하나의 가족으로"):
            self.assertIn(token, src)


class PrepareRetryApiTests(ServeMixin):
    """M2.11a/b: POST /api/integration/prepare-retry {run, batch,
    by, confirm} — one explicit human retry of a blocked prepare.

    Validation refuses before the lease; a non-blocked op refuses
    with 409; a busy lease refuses with 409 and starts nothing.
    The driver never calls this endpoint.
    """

    def _write_op(self, run_id, batch_id, status, error=""):
        from openmainty import batches as _batches
        from openmainty import store as _store
        from openmainty.util import write_json as _write_json
        _store.save({"id": run_id, "repo": "fakerepo", "dep": "foo",
                     "target": "1.2.3"})
        op_id = f"prep-{run_id}-{batch_id}"
        _write_json(_batches.op_file(op_id), {
            "op": "prepare", "op_id": op_id, "repo": "fakerepo",
            "run_id": run_id, "batch_id": batch_id, "dep": "foo",
            "target": "1.2.3", "lane": "next", "status": status,
            "error": error,
            "candidate": {"head": "c" * 40, "commit_list": ["c" * 40]},
            "base": {"batch": batch_id, "head": "b" * 40}})
        return op_id

    def test_missing_confirm_400(self):
        code, body = self.post("/api/integration/prepare-retry",
                               {"run": "run-x", "batch": "batch-x",
                                "by": "root"})
        self.assertEqual(code, 400)

    def test_worker_400_starts_nothing(self):
        code, body = self.post("/api/integration/prepare-retry",
                               {"run": "run-x", "batch": "batch-x",
                                "by": "worker", "confirm": True})
        self.assertEqual(code, 400)
        self.assertIn("owner", body.get("error", ""))

    def test_missing_ids_400(self):
        code, _ = self.post("/api/integration/prepare-retry",
                            {"by": "root", "confirm": True})
        self.assertEqual(code, 400)

    def test_not_blocked_409(self):
        self._write_op("run-done1", "batch-d", "done")
        code, body = self.post("/api/integration/prepare-retry",
                               {"run": "run-done1", "batch": "batch-d",
                                "by": "root", "confirm": True})
        self.assertEqual(code, 409)
        self.assertIn("blocked", body.get("error", ""))

    def test_busy_lease_409_starts_nothing(self):
        self._write_op("run-busy1", "batch-b", "blocked",
                       "cherry-pick conflicted")
        owner = sched.acquire_worker_lease(owner="test-holder")
        self.assertIsNotNone(owner)
        self.addCleanup(sched.release_worker_lease, owner)
        code, body = self.post("/api/integration/prepare-retry",
                               {"run": "run-busy1", "batch": "batch-b",
                                "by": "root", "confirm": True})
        self.assertEqual(code, 409)

    def test_happy_path_200_and_event(self):
        from openmainty import events as _events
        op_id = self._write_op("run-ok1", "batch-o", "blocked",
                               "cherry-pick conflicted")
        seen = {}

        def fake(run_id, batch_id, retry=False, origin="ui"):
            seen.update(run=run_id, batch=batch_id, retry=retry,
                        origin=origin)
            return {"op_id": op_id + "-rederive", "op": "reresolve",
                    "status": "done", "kind": "rederive"}

        real = serve.batches.prepare_integration
        serve.batches.prepare_integration = fake
        try:
            code, body = self.post("/api/integration/prepare-retry",
                                   {"run": "run-ok1", "batch": "batch-o",
                                    "by": "root", "confirm": True})
        finally:
            serve.batches.prepare_integration = real
        self.assertEqual(code, 200)
        self.assertEqual(body.get("status"), "done")
        self.assertTrue(seen.get("retry"))
        self.assertEqual(seen.get("origin"), "ui")
        found = [e for e in _events.list_events("fakerepo")
                 if e.get("kind") == "m2-prepare-retry"]
        self.assertTrue(found)
        self.assertIn("by root", found[-1].get("title") or "")
        self.assertIn(op_id, found[-1].get("detail") or "")
        self.assertIn("done", found[-1].get("detail") or "")
        self.assertEqual(found[-1].get("actor"), "root")

    def test_dashboard_posts_prepare_retry_with_confirm(self):
        src = (Path(__file__).resolve().parent.parent / "src"
               / "openmainty" / "dashboard.html").read_text(
                   encoding="utf-8")
        for token in ("prep-retry-go", "/api/integration/prepare-retry",
                      "data-prep-by", "prepRetryMsg",
                      "파생 출처 준비됨(원본과 다름)"):
            self.assertIn(token, src)


class JointRetryApiTests(ServeMixin):
    """M2.14b-fix Defect 1: POST /api/integration/joint-retry through
    the handler itself. The handler once called bare
    acquire_worker_lease/release_worker_lease, which do not exist in
    serve.py — every call 500'd with NameError. These tests execute
    the lease lines.
    """

    def _payload(self, **kw):
        base = {"run": "run-jx", "partner_run": "run-jy",
                "batch": "batch-j", "by": "root", "confirm": True}
        base.update(kw)
        return base

    def test_worker_400(self):
        with self.assertRaises(serve.ApiError) as ctx:
            serve.api_integration_joint_retry(self._payload(by="worker"))
        self.assertEqual(ctx.exception.status, 400)

    def test_missing_confirm_400(self):
        with self.assertRaises(serve.ApiError) as ctx:
            serve.api_integration_joint_retry(
                self._payload(confirm=False))
        self.assertEqual(ctx.exception.status, 400)

    def test_missing_partner_run_400(self):
        with self.assertRaises(serve.ApiError) as ctx:
            serve.api_integration_joint_retry(
                self._payload(partner_run=""))
        self.assertEqual(ctx.exception.status, 400)

    def test_busy_lease_409_starts_nothing(self):
        called = []

        def fake(*args, **kwargs):
            called.append((args, kwargs))
            raise AssertionError("joint_retry must not run on a "
                                 "busy lease")

        real = serve.batches.joint_retry
        serve.batches.joint_retry = fake
        try:
            with mock.patch.object(
                    sched, "acquire_worker_lease",
                    return_value=None):
                with self.assertRaises(serve.ApiError) as ctx:
                    serve.api_integration_joint_retry(self._payload())
        finally:
            serve.batches.joint_retry = real
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(called, [])

    def test_success_returns_record_and_releases(self):
        rec = {"op_id": "j1", "status": "done"}
        seen = {}

        def fake(run_id, partner_run_id, batch_id, by=None):
            seen.update(by=by)
            return rec

        real = serve.batches.joint_retry
        serve.batches.joint_retry = fake
        released = []
        real_release = sched.release_worker_lease

        def spy(token):
            released.append(token)
            return real_release(token)

        try:
            with mock.patch.object(sched, "release_worker_lease",
                                   side_effect=spy):
                out = serve.api_integration_joint_retry(
                    self._payload())
        finally:
            serve.batches.joint_retry = real
        self.assertEqual(out, rec)
        self.assertEqual(seen.get("by"), "root")
        self.assertEqual(len(released), 1)
        self.assertTrue(released[0])

    def test_joint_retry_error_409_releases(self):
        from openmainty import batches as _batches

        def fake(*args, **kwargs):
            raise _batches.IntegrationError("stale pair",
                                            http_status=409)

        real = serve.batches.joint_retry
        serve.batches.joint_retry = fake
        try:
            with self.assertRaises(serve.ApiError) as ctx:
                serve.api_integration_joint_retry(self._payload())
        finally:
            serve.batches.joint_retry = real
        self.assertEqual(ctx.exception.status, 409)
        probe = sched.acquire_worker_lease(owner="probe-after-409")
        self.assertIsNotNone(probe)
        sched.release_worker_lease(probe)

    def test_route_wired(self):
        code, body = self.post("/api/integration/joint-retry",
                               {"run": "run-jx", "batch": "batch-j",
                                "by": "root", "confirm": True})
        self.assertEqual(code, 400)
        self.assertIn("partner_run", body.get("error", ""))


class JointRefreshApiTests(ServeMixin):
    """M2.17b B: POST /api/integration/joint-refresh through the
    handler itself (owner/root manual lever; the driver path is
    the tick divert and never calls this).
    """

    def _payload(self, **kw):
        base = {"x_op": "prep-jx-base-1", "y_run": "run-jy",
                "base": "base-1", "by": "root", "confirm": True}
        base.update(kw)
        return base

    def _x_op(self, run="run-jx"):
        from openmainty import batches
        from openmainty.util import write_json
        write_json(batches.op_file("prep-jx-base-1"),
                   {"op": "prepare", "op_id": "prep-jx-base-1",
                    "repo": "fakerepo", "run_id": run,
                    "batch_id": "base-1", "lane": "next",
                    "status": "done",
                    "candidate": {"head": "xhead"}})

    def test_worker_400(self):
        with self.assertRaises(serve.ApiError) as ctx:
            serve.api_integration_joint_refresh(
                self._payload(by="worker"))
        self.assertEqual(ctx.exception.status, 400)

    def test_missing_confirm_400(self):
        with self.assertRaises(serve.ApiError) as ctx:
            serve.api_integration_joint_refresh(
                self._payload(confirm=False))
        self.assertEqual(ctx.exception.status, 400)

    def test_busy_lease_409_starts_nothing(self):
        self._x_op()
        called = []

        def fake(*args, **kwargs):
            called.append((args, kwargs))
            raise AssertionError("derive must not run on a busy lease")

        real = serve.batches.derive_joint_refresh
        serve.batches.derive_joint_refresh = fake
        try:
            with mock.patch.object(
                    sched, "acquire_worker_lease",
                    return_value=None):
                with self.assertRaises(serve.ApiError) as ctx:
                    serve.api_integration_joint_refresh(
                        self._payload())
        finally:
            serve.batches.derive_joint_refresh = real
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(called, [])

    def test_covered_409_starts_nothing(self):
        self._x_op()
        from openmainty import batches
        from openmainty.util import write_json
        at = "2026-10-24T01:00:00+00:00"
        write_json(batches.op_file("prep-cover-joint"),
                   {"op": "reresolve", "op_id": "prep-cover-joint",
                    "derived_from": "prep-jx-base-1", "kind": "joint",
                    "runs": ["run-jx", "run-jy"],
                    "repo": "fakerepo", "run_id": "run-jy",
                    "batch_id": "base-1", "lane": "next",
                    "status": "done", "created_at": at,
                    "updated_at": at,
                    "result": {"head": "h", "fingerprint": "f"}})
        called = []
        real = serve.batches.derive_joint_refresh
        serve.batches.derive_joint_refresh = (
            lambda *a, **k: called.append((a, k)))
        try:
            with self.assertRaises(serve.ApiError) as ctx:
                serve.api_integration_joint_refresh(
                    self._payload())
        finally:
            serve.batches.derive_joint_refresh = real
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(called, [])

    def test_refused_derive_409(self):
        self._x_op()
        from openmainty import batches as _batches

        def fake(*args, **kwargs):
            raise _batches.IntegrationError("base moved; refusing")

        real = serve.batches.derive_joint_refresh
        serve.batches.derive_joint_refresh = fake
        try:
            with self.assertRaises(serve.ApiError) as ctx:
                serve.api_integration_joint_refresh(
                    self._payload())
        finally:
            serve.batches.derive_joint_refresh = real
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("base moved", str(ctx.exception))

    def test_success_returns_done_joint_id(self):
        self._x_op()
        rec = {"op_id": "prep-jx-base-1-joint-expo-58.0.19",
               "status": "done", "kind": "joint"}
        seen = {}

        def fake(repo, lane, base_id, x_op_id, y_run_id, by="m2"):
            seen.update(repo=repo, by=by)
            return rec

        real = serve.batches.derive_joint_refresh
        serve.batches.derive_joint_refresh = fake
        try:
            out = serve.api_integration_joint_refresh(
                self._payload())
        finally:
            serve.batches.derive_joint_refresh = real
        self.assertEqual(out, rec)
        self.assertTrue(
            out["op_id"].endswith("-joint-expo-58.0.19"))
        self.assertEqual(seen.get("by"), "root")

    def test_driver_never_calls_refresh_api(self):
        src = (Path(__file__).resolve().parent.parent / "src" /
               "openmainty" / "m2.py").read_text(encoding="utf-8")
        self.assertNotIn("joint-refresh", src)

    def test_route_wired(self):
        code, body = self.post("/api/integration/joint-refresh",
                               {"x_op": "prep-jx-base-1",
                                "by": "root", "confirm": True})
        self.assertEqual(code, 400)
        self.assertIn("y_run", body.get("error", ""))


class QAPackApiTests(ServeMixin):
    """M2.18: POST /api/batch/qa-pack builds under the worker
    lease; GET returns the newest pack. Nothing attests."""

    def _pack(self, **kw):
        base = {"batch": "bid-qa", "at": "t", "by": "owner",
                "suggested_notes": {"diffcheck": "note"}}
        base.update(kw)
        return base

    def test_worker_400(self):
        with self.assertRaises(serve.ApiError) as ctx:
            serve.api_batch_qa_pack({"batch": "bid-qa",
                                     "by": "worker",
                                     "confirm": True})
        self.assertEqual(ctx.exception.status, 400)

    def test_missing_confirm_400(self):
        with self.assertRaises(serve.ApiError) as ctx:
            serve.api_batch_qa_pack({"batch": "bid-qa",
                                     "by": "owner",
                                     "confirm": False})
        self.assertEqual(ctx.exception.status, 400)

    def test_busy_409_starts_nothing(self):
        called = []

        def fake(*args, **kwargs):
            called.append((args, kwargs))
            raise AssertionError("pack must not build on busy lease")

        real = serve.batches.build_qa_pack
        serve.batches.build_qa_pack = fake
        try:
            with mock.patch.object(
                    sched, "acquire_worker_lease",
                    return_value=None):
                with self.assertRaises(serve.ApiError) as ctx:
                    serve.api_batch_qa_pack({"batch": "bid-qa",
                                             "by": "owner",
                                             "confirm": True})
        finally:
            serve.batches.build_qa_pack = real
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(called, [])

    def test_unknown_batch_409(self):
        with self.assertRaises(serve.ApiError) as ctx:
            serve.api_batch_qa_pack({"batch": "nope-missing",
                                     "by": "root", "confirm": True})
        self.assertEqual(ctx.exception.status, 409)

    def test_success_returns_pack(self):
        pack = self._pack()
        seen = {}

        def fake(batch_id, by=""):
            seen.update(batch=batch_id, by=by)
            return pack

        real = serve.batches.build_qa_pack
        serve.batches.build_qa_pack = fake
        try:
            out = serve.api_batch_qa_pack({"batch": "bid-qa",
                                           "by": "owner",
                                           "confirm": True})
        finally:
            serve.batches.build_qa_pack = real
        self.assertIs(out, pack)
        self.assertEqual(seen, {"batch": "bid-qa", "by": "owner"})

    def test_get_newest_and_missing(self):
        from openmainty import batches
        from openmainty.util import write_json
        qdir = batches.qa_dir("bid-qa")
        qdir.mkdir(parents=True, exist_ok=True)
        write_json(qdir / "pack-20200101000000.json",
                   {"batch": "bid-qa", "at": "old"})
        write_json(qdir / "pack-20210101000000.json",
                   {"batch": "bid-qa", "at": "new"})
        out = serve.api_batch_qa_pack_get({"batch": "bid-qa"})
        self.assertEqual(out["at"], "new")
        with self.assertRaises(serve.ApiError) as ctx:
            serve.api_batch_qa_pack_get({"batch": "bid-empty"})
        self.assertEqual(ctx.exception.status, 404)

    def test_get_route_wired(self):
        code, body = self.get("/api/batch/qa-pack?batch=bid-empty")
        self.assertEqual(code, 404)


class BranchProjectionTests(ServeMixin):
    """Overview projects the stored scan-time observation, never live git.

    has_next is True/False only on a scan_ok True record with a
    sane, matching, fresh observation; every legacy, missing,
    mismatched, stale, future, invalid, in-progress, or failed
    state projects None. The tests go through the real HTTP route.
    """

    def _seed_branch_state(self, **fields):
        st = sched.load("fakerepo")
        st.update(fields)
        sched.save(st)

    def _row(self):
        offline_service_status(self, {"installed": True, "running": False,
                                      "pid": None,
                                      "note": "branch projection fixture"})
        code, body = self.get("/api/overview")
        self.assertEqual(code, 200)
        rows = [r for r in body["repos"] if r["name"] == "fakerepo"]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def _no_branch_git(self):
        git_argv = []
        import openmainty.util as _util
        real_run = _util.run_cmd

        def spy(cmd, **kw):
            git_argv.append(list(cmd))
            return real_run(cmd, **kw)

        return (mock.patch("openmainty.util.run_cmd", side_effect=spy),
                mock.patch("openmainty.branches.probe_next",
                           side_effect=AssertionError(
                               "overview must not probe branches")),
                git_argv)

    def test_overview_branch_unknown_without_observation_no_branch_git(self):
        run_patch, probe_patch, git_argv = self._no_branch_git()
        with run_patch, probe_patch:
            row = self._row()
        self.assertIsNone(row["has_next"])
        self.assertIsNone(row["branch_next_observed_at"])
        self.assertEqual(row["branch_next_reason"], "마지막 스캔 실패")
        self.assertFalse(any("branch" in a for argv in git_argv
                             for a in argv),
                         f"branch git spawned: {git_argv}")

    def test_overview_branch_true_from_fresh_state_no_probe(self):
        from openmainty.util import utcnow
        now = utcnow()
        self._seed_branch_state(enabled=True, scan_ok=True,
                               branch_next=True, branch_next_at=now,
                               last_scan=now,
                               branch_next_checkout=str(self.checkout))
        _, probe_patch, _ = self._no_branch_git()
        with probe_patch:
            row = self._row()
        self.assertTrue(row["has_next"] is True)
        self.assertEqual(row["branch_next_observed_at"], now)
        self.assertEqual(row["branch_next_reason"], "마지막 스캔 관찰")

    def test_overview_branch_false_projects_false(self):
        from openmainty.util import utcnow
        now = utcnow()
        self._seed_branch_state(enabled=True, scan_ok=True,
                               branch_next=False, branch_next_at=now,
                               last_scan=now,
                               branch_next_checkout=str(self.checkout))
        _, probe_patch, _ = self._no_branch_git()
        with probe_patch:
            row = self._row()
        self.assertTrue(row["has_next"] is False)
        self.assertEqual(row["branch_next_reason"], "마지막 스캔 관찰")

    def test_overview_branch_stale_projects_unknown(self):
        old = "2020-01-01T00:00:00+00:00"
        self._seed_branch_state(enabled=True, scan_ok=True,
                               branch_next=True, branch_next_at=old,
                               last_scan=old,
                               branch_next_checkout=str(self.checkout))
        self.assertIsNone(self._row()["has_next"])

    def test_overview_branch_future_projects_unknown(self):
        future = "2099-01-01T00:00:00+00:00"
        self._seed_branch_state(enabled=True, scan_ok=True,
                               branch_next=True, branch_next_at=future,
                               last_scan=future,
                               branch_next_checkout=str(self.checkout))
        self.assertIsNone(self._row()["has_next"])

    def test_overview_branch_malformed_stamp_projects_unknown(self):
        self._seed_branch_state(enabled=True, scan_ok=True,
                               branch_next=True,
                               branch_next_at="not-a-time",
                               last_scan="not-a-time",
                               branch_next_checkout=str(self.checkout))
        self.assertIsNone(self._row()["has_next"])

    def test_overview_branch_mismatched_checkout_projects_unknown(self):
        from openmainty.util import utcnow
        now = utcnow()
        self._seed_branch_state(enabled=True, scan_ok=True,
                               branch_next=True, branch_next_at=now,
                               last_scan=now,
                               branch_next_checkout="/elsewhere")
        row = self._row()
        self.assertIsNone(row["has_next"])
        self.assertEqual(row["branch_next_reason"], "체크아웃 변경됨")

    def test_overview_branch_failed_scan_projects_unknown(self):
        from openmainty.util import utcnow
        now = utcnow()
        self._seed_branch_state(enabled=True, scan_ok=False,
                               branch_next=True, branch_next_at=now,
                               last_scan=now,
                               branch_next_checkout=str(self.checkout))
        row = self._row()
        self.assertIsNone(row["has_next"])
        self.assertEqual(row["branch_next_reason"], "마지막 스캔 실패")

    def test_overview_branch_string_scan_ok_projects_unknown(self):
        from openmainty.util import utcnow
        now = utcnow()
        self._seed_branch_state(enabled=True, scan_ok="false",
                               branch_next=True, branch_next_at=now,
                               last_scan=now,
                               branch_next_checkout=str(self.checkout))
        self.assertIsNone(self._row()["has_next"])

    def test_overview_branch_in_progress_projects_unknown(self):
        from openmainty.util import utcnow
        now = utcnow()
        self._seed_branch_state(enabled=True, scan_ok=True,
                               branch_next=True, branch_next_at=now,
                               last_scan=now,
                               branch_next_checkout=str(self.checkout),
                               current_job={"id": "k0"})
        row = self._row()
        self.assertIsNone(row["has_next"])
        self.assertEqual(row["branch_next_reason"], "스캔 상태 불안정")


if __name__ == "__main__":
    unittest.main()
