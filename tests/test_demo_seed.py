"""Durable coverage for the synthetic offline demo home (docs/DEMO.md).

Seeds into temp dirs only, serves on a loopback ephemeral port, and
asserts no network beyond 127.0.0.1 and no writes outside the target.
"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import demo, repos, serve, store  # noqa: E402
try:
    from tests._home import (use_temp_home, offline_muse_version,  # noqa: E402
                             offline_muse_binary)
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home, offline_muse_version, offline_muse_binary  # noqa: E402


class DemoSeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.demo = str(Path(self.tmp.name) / "demo")
        # Point at the (about to be seeded) demo dir up front: seed()
        # restores the previous value when it returns.
        use_temp_home(self, self.demo)
        # Deterministic regardless of host PATH: stub CLI presence.
        offline_muse_binary(self, "/fixture/muse")

    def test_seed_creates_expected_runs_and_states(self):
        info = demo.seed(self.demo)
        self.assertEqual(info["repos"],
                         [demo.REPO, demo.EMPTY_REPO])
        self.assertEqual(info["runs"]["waiting"], demo.WAITING_RUN_ID)
        waiting = store.load(demo.WAITING_RUN_ID)
        self.assertTrue(
            (waiting.get("waitReason") or "").startswith("question:"))
        analysis = (((waiting["steps"]["analyze"].get("usage") or {})
                     .get("terminal_text")) or "")
        self.assertIn("| 모듈 | 상태 | 파일 |", analysis)
        self.assertIn("workfile:demo_button.ts", analysis)
        self.assertIn("```", analysis)
        done = store.load(demo.DONE_RUN_ID)
        self.assertEqual(done["steps"]["fix"]["status"], "done")
        # F9: a real finished run has every step done (`_run_finished`
        # and the "완료" label both require it).
        for name, step in done["steps"].items():
            self.assertEqual(step["status"], "done", name)
        patch = (store.run_dir(demo.DONE_RUN_ID) / "fix.diff.txt")
        self.assertTrue(patch.is_file())
        self.assertIn("demo_button.ts", patch.read_text(encoding="utf-8"))
        missing = store.load(demo.MISSING_RUN_ID)
        self.assertFalse((store.run_dir(demo.MISSING_RUN_ID) / "work")
                         .exists())
        self.assertEqual(missing["issue"], "9")
        # F4: the stored patch parses to exactly one file.
        diffstat = (store.run_dir(demo.DONE_RUN_ID) / "fix.diffstat.txt")
        self.assertTrue(diffstat.is_file())
        self.assertIn("demo_button.ts",
                      diffstat.read_text(encoding="utf-8"))

    def test_missing_run_is_unavailable_not_planned(self):
        # F3: steps ran but the work folder is gone with no stored
        # patch, so the product reports unavailable — never planned.
        demo.seed(self.demo)
        missing = store.load(demo.MISSING_RUN_ID)
        self.assertEqual(missing["steps"]["triage"]["status"], "done")
        self.assertNotEqual(
            missing["steps"]["analyze"]["status"], "pending")
        # F9: the runner never writes "error" — TERMINAL is
        # {done, failed, blocked}. A ran-then-lost step is failed.
        self.assertEqual(
            missing["steps"]["analyze"]["status"], "failed")
        self.assertFalse((store.run_dir(demo.MISSING_RUN_ID) / "work")
                         .exists())
        sc = serve._source_changes(missing)
        self.assertEqual(sc["availability"], "unavailable")
        self.assertFalse(sc["hasChanges"])
        self.assertFalse(sc["planned"])
        self.assertIn("run work dir missing", sc["error"])

    def test_done_run_stored_patch_counts_one_file(self):
        # F4: model claim (terminal text) and stored evidence agree.
        demo.seed(self.demo)
        done = store.load(demo.DONE_RUN_ID)
        sc = serve._source_changes(done)
        self.assertEqual(sc["availability"], "historical")
        self.assertEqual(len(sc["fileDiffs"]), 1)
        self.assertEqual(sc["fileDiffs"][0]["path"], "demo_button.ts")
        self.assertIn("1 file changed", sc["filesTotal"])

    def test_seed_refuses_non_empty_dir(self):
        Path(self.demo).mkdir(parents=True)
        (Path(self.demo) / "existing.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(SystemExit):
            demo.seed(self.demo)
        # The pre-existing file is untouched and nothing was added.
        self.assertEqual(
            sorted(p.name for p in Path(self.demo).iterdir()),
            ["existing.txt"])

    def test_seed_writes_nothing_outside_target(self):
        parent = Path(self.tmp.name)
        before = sorted(p.name for p in parent.iterdir())
        demo.seed(self.demo)
        after = sorted(p.name for p in parent.iterdir())
        # Only the "demo" target dir is new; the "home" fixture dir
        # already existed via setUp.
        self.assertEqual(after, sorted(before + ["demo"]))
        for p in Path(self.demo).rglob("*"):
            self.assertTrue(self._inside(p, Path(self.demo)))

    @staticmethod
    def _inside(path, root):
        try:
            Path(path).resolve().relative_to(root.resolve())
            return True
        except ValueError:
            return False


class DemoServeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.demo = str(Path(self.tmp.name) / "demo")
        demo.seed(self.demo)
        use_temp_home(self, self.demo)
        offline_muse_version(self, "fixture-muse-0.0.0")
        from openmainty import muse as _muse
        which_patch = mock.patch.object(
            _muse, "which", return_value="/fixture/muse")
        which_patch.start()
        self.addCleanup(which_patch.stop)
        serve._JOBS.clear()
        serve._JOB_CANCEL.clear()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)

    def _stop_server(self):
        self.httpd.shutdown()
        self.thread.join(timeout=15)
        self.httpd.server_close()

    def _get(self, path):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}{path}",
                timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())

    def test_overview_lists_fixture_repo_and_runs(self):
        code, body = self._get("/api/overview")
        self.assertEqual(code, 200)
        names = [r["name"] for r in body["repos"]]
        self.assertIn(demo.REPO, names)
        self.assertIn(demo.EMPTY_REPO, names)
        run_ids = [r["id"] for r in body["runs"]]
        self.assertIn(demo.WAITING_RUN_ID, run_ids)

    def test_work_waiting_run_shows_question_and_report(self):
        code, body = self._get(
            f"/api/work?run={demo.WAITING_RUN_ID}")
        self.assertEqual(code, 200)
        self.assertTrue(
            ((body.get("state") or {}).get("waitReason") or "")
            .startswith("question:"))
        analysis = (body.get("report") or {}).get("analysis") or ""
        self.assertIn("| 모듈 | 상태 | 파일 |", analysis)

    def test_work_missing_run_reports_unavailable_over_http(self):
        # F3 end to end: the seeded missing run really reads as
        # unavailable through the served API, not just in-process.
        code, body = self._get(
            f"/api/work?run={demo.MISSING_RUN_ID}")
        self.assertEqual(code, 200)
        sc = body.get("sourceChanges") or {}
        self.assertEqual(sc.get("availability"), "unavailable")
        self.assertIn("run work dir missing", sc.get("error", ""))

    def test_work_done_run_reports_one_stored_file_over_http(self):
        # F4 end to end: the seeded done run counts one stored file.
        code, body = self._get(
            f"/api/work?run={demo.DONE_RUN_ID}")
        self.assertEqual(code, 200)
        sc = body.get("sourceChanges") or {}
        self.assertEqual(sc.get("availability"), "historical")
        self.assertEqual(len(sc.get("fileDiffs") or []), 1)
        self.assertIn("1 file changed", sc.get("filesTotal", ""))

    def test_work_step_shapes_match_real_finished_shape_over_http(self):
        # F9/F10 end to end: the done run reads all-done (the finished
        # shape), the missing run reads analyze failed with no work dir.
        code, body = self._get(
            f"/api/work?run={demo.DONE_RUN_ID}")
        self.assertEqual(code, 200)
        for name, step in (
                (body.get("state") or {}).get("steps", {}).items()):
            self.assertEqual(step.get("status"), "done", name)
        code, body = self._get(
            f"/api/work?run={demo.MISSING_RUN_ID}")
        self.assertEqual(code, 200)
        steps = (body.get("state") or {}).get("steps", {})
        self.assertEqual(steps.get("triage", {}).get("status"), "done")
        self.assertEqual(steps.get("analyze", {}).get("status"), "failed")

    def test_demo_stopped_run_reports_nothing_running(self):
        # F15: triage done, analyze pending, no waitReason, no failure,
        # no stored patch, no work dir — the exact inputs from which
        # runOutcomeWord derives the stopped label.
        stopped = store.load(demo.STOPPED_RUN_ID)
        self.assertEqual(stopped["issue"], "5")
        self.assertIsNone(stopped.get("waitReason"))
        self.assertEqual(
            stopped["steps"]["triage"]["status"], "done")
        self.assertEqual(
            stopped["steps"]["analyze"]["status"], "pending")
        for name, step in stopped["steps"].items():
            self.assertNotIn(step.get("status"),
                             ("running", "failed", "blocked"), name)
        self.assertFalse(
            (store.run_dir(demo.STOPPED_RUN_ID) / "work").exists())

    def test_work_stopped_shape_over_http(self):
        # F15: over HTTP the stopped run shows triage done and analyze
        # pending with nothing live; the "멈춤" label itself is derived
        # client-side by runOutcomeWord (covered in test_work_ui_logic).
        code, body = self._get(
            f"/api/work?run={demo.STOPPED_RUN_ID}")
        self.assertEqual(code, 200)
        steps = (body.get("state") or {}).get("steps", {})
        self.assertEqual(steps.get("triage", {}).get("status"), "done")
        self.assertEqual(
            steps.get("analyze", {}).get("status"), "pending")
        live = [v for v in steps.values()
                if v.get("status") in ("running", "failed", "blocked")]
        self.assertEqual(live, [])

    def test_stopped_run_first_pending_step_for_resume_hint(self):
        # F16: the stopped shape the next-chain branch keys on — no wait,
        # no failure, nothing live — names reproduce as the first non-done
        # step, which is what the 이어하기 hint offers to continue from.
        code, body = self._get(
            f"/api/work?run={demo.STOPPED_RUN_ID}")
        self.assertEqual(code, 200)
        steps = (body.get("state") or {}).get("steps", {})
        pending = [name for name, step in steps.items()
                   if step.get("status") != "done"]
        self.assertEqual(pending[0], "reproduce")
        self.assertIsNone((body.get("state") or {}).get("waitReason"))

    def test_graph_run_nodes_carry_run_id_for_member_words(self):
        # G2: the graph member panel keys runOutcomeWord off run_id; the
        # stopped/waiting/failed/done demo runs must all be present as
        # run nodes for the client-side mapping to reach them.
        code, body = self._get(f"/api/graph?repo={demo.REPO}")
        self.assertEqual(code, 200)
        runs = {n.get("run_id"): n.get("status")
                for n in (body.get("nodes") or [])
                if n.get("type") == "run"}
        for rid in (demo.STOPPED_RUN_ID, demo.WAITING_RUN_ID,
                    demo.DONE_RUN_ID, demo.MISSING_RUN_ID):
            self.assertIn(rid, runs)

    def test_demo_repos_report_distinct_github_slugs(self):
        # G6: the two demo repos must not share one GitHub slug.
        sample = repos.load(demo.REPO)
        empty = repos.load(demo.EMPTY_REPO)
        self.assertEqual(sample.get("github"), demo.GITHUB)
        self.assertEqual(empty.get("github"), demo.EMPTY_GITHUB)
        self.assertNotEqual(sample.get("github"), empty.get("github"))

    def test_graph_statuses_all_have_filter_labels(self):
        # G1b: every status the demo graph really carries must be
        # choosable in the filter, or the select misstates the view.
        code, body = self._get(f"/api/graph?repo={demo.REPO}")
        self.assertEqual(code, 200)
        statuses = {n.get("status")
                    for n in (body.get("nodes") or [])
                    if n.get("status")}
        self.assertTrue(statuses, "demo graph carries statuses")
        html = (Path(__file__).resolve().parent.parent
                / "src" / "openmainty" / "dashboard.html"
                ).read_text(encoding="utf-8")
        gf = html[html.index("const GF_STATUS_KO = "):
                  html.index("const EDGE_KO = ")]
        for st in sorted(statuses):
            self.assertIn(f"{st}:", gf,
                          f"graph status {st!r} needs a filter label")

    def test_digest_unknown_day_shape(self):
        # G4: a day with no stored digest reads unknown (never 500); the
        # client renders the honest empty state off this shape.
        code, body = self._get("/api/digest?date=2999-01-01")
        self.assertEqual(code, 200)
        self.assertTrue(body.get("unknown"))

    def test_answer_refused_without_muse_binary(self):
        from openmainty import muse as _muse
        with mock.patch.object(_muse, "which", return_value=None):
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/api/job/answer",
                data=json.dumps(
                    {"run": demo.WAITING_RUN_ID,
                     "answer": "fixture answer"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST")
            try:
                urllib.request.urlopen(req, timeout=30)
                self.fail("expected 403")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 403)
        # The refused answer wrote nothing: the question still waits.
        self.assertTrue(
            store.load(demo.WAITING_RUN_ID)["waitReason"]
            .startswith("question:"))


if __name__ == "__main__":
    unittest.main()
