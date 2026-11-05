"""Watch decisions and dashboard smoke tests (offline)."""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import os  # noqa: E402

from openmainty import artifacts, cli, store  # noqa: E402
try:
    from tests._home import use_temp_home, offline_service_status  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home, offline_service_status  # noqa: E402


class BatchingTests(unittest.TestCase):
    def test_drift_batches_into_one_run(self):
        rows = [
            {"name": "a", "kind": "npm", "id": "a", "pinned": "1.0.0",
             "latest": "2.0.0", "drift": True, "error": None},
            {"name": "b", "kind": "pub", "id": "b", "pinned": "1.0.0",
             "latest": "1.1.0", "drift": True, "error": None},
        ]
        entries = cli.drift_entries(rows)
        self.assertEqual(len(entries), 1, "drift must batch, not spray runs")
        key, title, body = entries[0]
        self.assertIn("1.0.0", body)
        self.assertIn("2.0.0", body)
        self.assertEqual(cli.drift_entries([]), [])

    def test_pr_draft_carries_suggested_labels(self):
        profile = {"name": "x", "github": "e/x", "labels": {"pr": ["openmainty", "deps"]}}
        state = {"id": "run-1", "repo": "x", "issue": "2", "title": "t",
                 "baseCommit": "a", "workCommit": "b", "verifiedCommits": {},
                 "steps": {"reproduce": {"status": "done"},
                           "review": {"status": "done"}, "e2e": {"status": "done"}}}
        body = artifacts.pr_description(state, profile, "s")
        self.assertIn("openmainty", body)
        self.assertIn("deps", body)


class WatchDecisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_temp_home(self, str(Path(self.tmp.name) / "home"))

    def _run(self, issue, statuses):
        state = store.create("r", issue, "t", "base0", {"worker": "test"})
        for step, status in statuses.items():
            store.mark(state, step, status, exit_code=0 if status == "done" else 1)
        store.save(state)
        return state

    def test_start_resume_skip(self):
        self.assertEqual(cli.decide_issue("r", "1"), "start")
        done = {s: "done" for s in store.STEP_ORDER}
        self._run("2", done)
        self.assertEqual(cli.decide_issue("r", "2"), "skip-done")
        partial = dict(done)
        partial["e2e"] = "failed"
        self._run("3", partial)
        self.assertEqual(cli.decide_issue("r", "3"), "resume")
        running = dict(done)
        running["fix"] = "running"
        self._run("4", running)
        self.assertEqual(cli.decide_issue("r", "4"), "skip-active")
        waiting = dict(done)
        waiting["review"] = "pending"
        st = self._run("5", waiting)
        st["waitReason"] = "subscription-quota at step review"
        store.save(st)
        self.assertEqual(cli.decide_issue("r", "5"), "skip-waiting")

    def test_reply_draft_reports_skip_draft(self):
        from openmainty import chat
        drafts = Path(self.tmp.name) / "home" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "rx-6.md").write_text("hello #7\n", encoding="utf-8")
        chat.sync_reply_session("r", "6", "reply", "rx-6.md")
        self.assertEqual(cli.decide_issue("r", "6"), "skip-draft")


class ServeTests(unittest.TestCase):
    def test_overview_and_run_endpoints(self):
        offline_service_status(self, {"installed": True, "running": False, "pid": None, "note": "fixture offline; scheduler discovery is not under test"})  # noqa: E501
        from openmainty import serve as _serve
        import urllib.request

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        use_temp_home(self, str(Path(tmp.name) / "home"))
        state = store.create("r", "9", "t", "base0", {"worker": "test"})
        httpd = _serve.ThreadingHTTPServer(("127.0.0.1", 0), _serve.Handler)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{port}"
            overview = json.loads(urllib.request.urlopen(base + "/api/overview", timeout=10).read())
            self.assertIn("repos", overview)
            self.assertIn("runs", overview)
            self.assertTrue(any(r["id"] == state["id"] for r in overview["runs"]))
            detail = json.loads(urllib.request.urlopen(
                base + f"/api/run?id={state['id']}", timeout=10).read())
            self.assertEqual(detail["id"], state["id"])
            page = urllib.request.urlopen(base + "/", timeout=10).read().decode()
            self.assertIn("OpenMainty", page)
        finally:
            httpd.shutdown()
            thread.join(timeout=10)


if __name__ == "__main__":
    unittest.main()
