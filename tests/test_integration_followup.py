"""Source-bound followup corrections: lifecycle, scoping, contention.

Fake git repos under an isolated OPENMAINTY_HOME; the subscription worker
(muse.exec_prompt) is scripted, never invoked. No network, no devices.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import batches, paths, runner, sched, serve  # noqa: E402
from openmainty.util import Cancelled, write_json  # noqa: E402
try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402


def git(*args, cwd):
    proc = subprocess.run(["git", "-C", str(cwd)] + list(args),
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr[-300:]}")
    return proc.stdout


class FollowupMixin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        use_temp_home(self, str(self.root / "home"))
        self.origin = self.root / "origin"
        self.origin.mkdir()
        git("init", "-b", "main", cwd=self.origin)
        git("config", "user.email", "test@localhost", cwd=self.origin)
        git("config", "user.name", "test", cwd=self.origin)
        (self.origin / "a.txt").write_text("base\n", encoding="utf-8")
        (self.origin / "Podfile.lock").write_text("P: 0.36.5\n",
                                                  encoding="utf-8")
        dist = self.origin / "example" / "dist"
        dist.mkdir(parents=True)
        (dist / "out.js").write_text("v1\n", encoding="utf-8")
        git("add", "-A", cwd=self.origin)
        git("commit", "-qm", "base", cwd=self.origin)
        git("checkout", "-qb", "op-branch", cwd=self.origin)
        (self.origin / "a.txt").write_text("integrated\n", encoding="utf-8")
        git("commit", "-qam", "prepared", cwd=self.origin)
        self.prep_head = git("rev-parse", "HEAD", cwd=self.origin).strip()
        git("checkout", "-q", "main", cwd=self.origin)
        # Named-branch worktree: proves the recorded branch advances.
        self.wt = self.root / "home" / "reviews" / "prep-wt"
        git("worktree", "add", "-b", "op-wt", str(self.wt), self.prep_head,
            cwd=self.origin)
        snap = runner.source_snapshot(self.wt)
        self.op_id = "prep-run-f1-test-batch"
        write_json(batches.op_file(self.op_id), {
            "schema": 1, "op": "prepare", "op_id": self.op_id,
            "repo": "fakerepo", "run_id": "run-f1", "batch_id": "test-batch",
            "dep": "foo", "target": "1.2.3", "lane": "main",
            "candidate": {"head": "c" * 40, "fingerprint": "f" * 64},
            "base": {"batch": "test-batch", "head": "b" * 40,
                     "fingerprint": "g" * 64},
            "result": {"path": str(self.wt), "branch": "op-wt",
                       "head": self.prep_head,
                       "fingerprint": snap["fingerprint"]},
            "gates": [{"key": "source-prepared", "status": "pass",
                       "note": ""},
                      {"key": "fastchecks", "status": "pending", "note": ""},
                      {"key": "native", "status": "pending", "note": ""}],
            "status": "done", "error": "", "followups": [],
            "created_at": "t", "updated_at": "t"})
        self.exec_calls = []

    def fake_exec(self, result=(0, "FILES CHANGED\n- Podfile.lock\n", None),
                  edit=None):
        def _run(prompt, workdir=None, contributor=True, timeout=1800,
                 model=None, cancel_event=None):
            self.exec_calls.append({"prompt": prompt, "workdir": workdir,
                                    "contributor": contributor,
                                    "model": model})
            if edit:
                edit(Path(workdir))
            code, text, _ = result
            usage = {"model": "muse-spark-1.3",
                     "provider_model": "muse-spark-1.3-contributor",
                     "session_id": "sess-test", "request_id": "req-test",
                     "cli_version": "test", "exit_code": code}
            return code, text, usage
        return _run

    def op_record(self):
        return json.loads(batches.op_file(self.op_id).read_text(
            encoding="utf-8"))


class FollowupTests(FollowupMixin):
    def test_correction_commits_with_provenance(self):
        def edit(workdir):
            (workdir / "Podfile.lock").write_text("P: 0.37.1\n",
                                                  encoding="utf-8")
        with mock.patch("openmainty.muse.exec_prompt",
                         side_effect=self.fake_exec(edit=edit)):
            _, fid = batches.request_followup(self.op_id, "sync lockfile")
            rec = batches.execute_followup(self.op_id, fid)
        entry = [e for e in rec["followups"] if e["fid"] == fid][0]
        self.assertEqual(entry["status"], "done")
        self.assertEqual(entry["exit_code"], 0)
        self.assertTrue(entry["commit"]["committed"])
        self.assertNotEqual(rec["result"]["head"], self.prep_head)
        self.assertEqual(rec["active_followup"], "")
        gates = {g["key"]: g["status"] for g in rec["gates"]}
        self.assertEqual(gates[f"correction-{fid}"], "pass")
        # Preparation proof stands; correction carries its own gate.
        self.assertEqual(gates["source-prepared"], "pass")
        self.assertEqual(entry["usage"]["session_id"], "sess-test")
        # Commit message carries exact provenance on the op branch,
        # and the recorded branch actually advanced.
        log = git("log", "--format=%s%n%b", "-1", cwd=self.wt)
        self.assertIn(self.op_id, log)
        self.assertIn(fid, log)
        self.assertEqual(git("branch", "--show-current", cwd=self.wt).strip(),
                         "op-wt")
        self.assertIn(rec["result"]["head"],
                      git("rev-list", "op-wt", cwd=self.origin))
        # Worker stayed inside the worktree with source-only bounds.
        self.assertEqual(self.exec_calls[0]["workdir"], str(self.wt))
        self.assertTrue(self.exec_calls[0]["contributor"])
        prompt = self.exec_calls[0]["prompt"]
        self.assertIn(str(self.wt), prompt)
        self.assertIn("physical devices", prompt)
        self.assertIn("checkout", prompt)
        self.assertIn("rebase", prompt)
        self.assertIn("reset", prompt)
        self.assertIn("making commits yourself", prompt)
        self.assertIn("/opt/homebrew/opt/ruby/bin", prompt)
        self.assertIn("Gemfile.lock", prompt)

    def test_no_change_is_done_without_commit(self):
        before_log = git("rev-list", "--count", "HEAD", cwd=self.wt)
        with mock.patch("openmainty.muse.exec_prompt",
                         side_effect=self.fake_exec()):
            _, fid = batches.request_followup(self.op_id, "check only")
            rec = batches.execute_followup(self.op_id, fid)
        entry = [e for e in rec["followups"] if e["fid"] == fid][0]
        self.assertEqual(entry["status"], "done")
        self.assertEqual(rec["result"]["head"], self.prep_head)
        self.assertEqual(git("rev-list", "--count", "HEAD", cwd=self.wt),
                         before_log)

    def test_failed_model_records_blocked_leaves_evidence(self):
        def edit(workdir):
            (workdir / "a.txt").write_text("half\n", encoding="utf-8")
        with mock.patch("openmainty.muse.exec_prompt",
                         side_effect=self.fake_exec(result=(1, "boom", None),
                                                    edit=edit)):
            _, fid = batches.request_followup(self.op_id, "risky")
            rec = batches.execute_followup(self.op_id, fid)
        entry = [e for e in rec["followups"] if e["fid"] == fid][0]
        self.assertEqual(entry["status"], "blocked")
        self.assertIn("model exit 1", entry["error"])
        # Partial state preserved, nothing committed.
        self.assertEqual((self.wt / "a.txt").read_text(encoding="utf-8"),
                         "half\n")
        self.assertEqual(rec["result"]["head"], self.prep_head)

    def test_cancel_records_cancelled(self):
        def _raise(*args, **kwargs):
            raise Cancelled("stop")
        with mock.patch("openmainty.muse.exec_prompt", side_effect=_raise):
            _, fid = batches.request_followup(self.op_id, "long")
            with self.assertRaises(Cancelled):
                batches.execute_followup(self.op_id, fid)
        rec = self.op_record()
        entry = [e for e in rec["followups"] if e["fid"] == fid][0]
        self.assertEqual(entry["status"], "cancelled")
        self.assertEqual(rec["active_followup"], "")

    def test_overlap_second_request_refused(self):
        batches.request_followup(self.op_id, "first")
        with self.assertRaises(batches.IntegrationError):
            batches.request_followup(self.op_id, "second")

    def test_drifted_source_refused_before_start(self):
        (self.wt / "a.txt").write_text("drift\n", encoding="utf-8")
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.request_followup(self.op_id, "late")
        self.assertIn("drifted", str(ctx.exception))
        self.assertEqual(self.op_record().get("followups"), [])

    def test_unknown_op_and_empty_request(self):
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.request_followup("prep-nope-x", "hi")
        self.assertEqual(ctx.exception.http_status, 404)
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.request_followup(self.op_id, "   ")
        self.assertEqual(ctx.exception.http_status, 400)

    def test_endpoint_ignores_arbitrary_paths(self):
        with mock.patch.object(serve, "_require_gate", lambda: None):
            with mock.patch("openmainty.muse.exec_prompt",
                             side_effect=self.fake_exec()):
                out = serve.api_integration_followup(
                    {"op": self.op_id, "request": "sync",
                     "path": "/evil", "command": "rm -rf /",
                     "workdir": "/evil"})
                job_id = out["job"]["id"]
                deadline = time.time() + 60
                while time.time() < deadline:
                    rec = self.op_record()
                    if not rec.get("active_followup"):
                        break
                    time.sleep(0.5)
        rec = self.op_record()
        self.assertTrue(rec["result"]["path"].endswith("prep-wt"))
        self.assertNotIn("/evil", json.dumps(rec))
        self.assertEqual(out["op"], self.op_id)

    def test_branch_switch_rejected_before_start(self):
        git("checkout", "--detach", "HEAD", cwd=self.wt)
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.request_followup(self.op_id, "late")
        self.assertIn("branch switch", str(ctx.exception))
        self.assertEqual(self.op_record().get("followups"), [])

    def test_build_noise_never_staged(self):
        def edit(workdir):
            (workdir / "Podfile.lock").write_text("P: 0.37.1\n",
                                                  encoding="utf-8")
            (workdir / "example" / "dist" / "out.js").write_text(
                "v2-noisy\n", encoding="utf-8")
        with mock.patch("openmainty.muse.exec_prompt",
                         side_effect=self.fake_exec(edit=edit)):
            _, fid = batches.request_followup(self.op_id, "sync lockfile")
            rec = batches.execute_followup(self.op_id, fid)
        entry = [e for e in rec["followups"] if e["fid"] == fid][0]
        self.assertEqual(entry["status"], "done")
        committed_files = git("show", "--name-only", "--format=",
                              cwd=self.wt)
        self.assertIn("Podfile.lock", committed_files)
        self.assertNotIn("example/dist/out.js", committed_files)
        # Noise stays in the tree, out of history, source still clean.
        self.assertEqual((self.wt / "example" / "dist" / "out.js")
                         .read_text(encoding="utf-8"), "v2-noisy\n")
        self.assertTrue(entry["after"]["clean"])

    def test_model_self_commit_adopted_on_branch(self):
        def edit(workdir):
            (workdir / "Podfile.lock").write_text("P: 0.37.1\n",
                                                  encoding="utf-8")
            git("-c", "user.name=t", "-c", "user.email=t@t", "add", "-A",
                cwd=workdir)
            git("-c", "user.name=t", "-c", "user.email=t@t", "commit",
                "-qm", "model did it", cwd=workdir)
        with mock.patch("openmainty.muse.exec_prompt",
                         side_effect=self.fake_exec(edit=edit)):
            _, fid = batches.request_followup(self.op_id, "sync lockfile")
            rec = batches.execute_followup(self.op_id, fid)
        entry = [e for e in rec["followups"] if e["fid"] == fid][0]
        self.assertEqual(entry["status"], "done")
        self.assertTrue(entry["commit"].get("adopted"))
        self.assertEqual(rec["result"]["head"],
                         entry["commit"]["head"])
        self.assertNotEqual(rec["result"]["head"], self.prep_head)

    def test_model_offbranch_commit_blocked(self):
        def edit(workdir):
            git("checkout", "-qb", "rogue", cwd=workdir)
            (workdir / "Podfile.lock").write_text("P: 9.9.9\n",
                                                  encoding="utf-8")
            git("-c", "user.name=t", "-c", "user.email=t@t", "add", "-A",
                cwd=workdir)
            git("-c", "user.name=t", "-c", "user.email=t@t", "commit",
                "-qm", "rogue", cwd=workdir)
        with mock.patch("openmainty.muse.exec_prompt",
                         side_effect=self.fake_exec(edit=edit)):
            _, fid = batches.request_followup(self.op_id, "sync lockfile")
            rec = batches.execute_followup(self.op_id, fid)
        entry = [e for e in rec["followups"] if e["fid"] == fid][0]
        self.assertEqual(entry["status"], "blocked")
        self.assertEqual(rec["result"]["head"], self.prep_head)

    def test_terminal_report_extracted_not_raw_json(self):
        raw = ('{"payload": {"kind": "run_output_delta", "text": "hi"}}\n'
               '{"payload": {"kind": "run_terminal", "text": '
               '"FILES CHANGED\\n- Podfile.lock"}}')
        with mock.patch("openmainty.muse.exec_prompt",
                         side_effect=self.fake_exec(result=(0, raw, None))):
            _, fid = batches.request_followup(self.op_id, "sync lockfile")
            rec = batches.execute_followup(self.op_id, fid)
        entry = [e for e in rec["followups"] if e["fid"] == fid][0]
        self.assertEqual(entry["report"], "FILES CHANGED\n- Podfile.lock")
        self.assertNotIn("payload", entry["log_tail"])
        self.assertNotIn("payload", entry["report"])
        self.assertIsNone(entry["usage"].get("tokens", None) or None)
        self.assertIn("unmeasurable", entry["token_note"])

    def test_request_too_long_rejected(self):
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.request_followup(self.op_id, "x" * 2001)
        self.assertEqual(ctx.exception.http_status, 400)

    def test_orphan_running_settled_for_audit(self):
        opf = batches.op_file(self.op_id)
        rec = self.op_record()
        rec["followups"].append({"fid": "fu-deadbeef", "request": "old",
                                 "status": "running", "before": {},
                                 "after": {}, "exit_code": None,
                                 "usage": {}, "error": "",
                                 "created_at": "t", "updated_at": "t"})
        rec["active_followup"] = "fu-deadbeef"
        write_json(opf, rec)
        _, fid = batches.request_followup(self.op_id, "retry")
        self.assertNotEqual(fid, "fu-deadbeef")
        rec = self.op_record()
        old = [e for e in rec["followups"]
               if e["fid"] == "fu-deadbeef"][0]
        self.assertEqual(old["status"], "blocked")
        self.assertIn("orphan", old["error"])

    def test_job_rc_mapping(self):
        self.assertEqual(serve._followup_job_rc(
            {"followups": [{"fid": "fu-1", "status": "done"}]}, "fu-1"), 0)
        self.assertEqual(serve._followup_job_rc(
            {"followups": [{"fid": "fu-1", "status": "blocked"}]}, "fu-1"),
            1)
        self.assertEqual(serve._followup_job_rc({"followups": []}, "fu-1"),
                         1)

    def test_no_job_slot_rolls_back_and_releases(self):
        with mock.patch.object(serve, "_job_new", return_value=None):
            with mock.patch.object(serve, "_require_gate", lambda: None):
                with self.assertRaises(serve.ApiError) as ctx:
                    serve.api_integration_followup(
                        {"op": self.op_id, "request": "sync"})
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("nothing queued", str(ctx.exception.message))
        rec = self.op_record()
        self.assertEqual(rec.get("followups"), [])
        self.assertEqual(rec.get("active_followup") or "", "")
        token = sched.acquire_worker_lease("probe-after-no-slot")
        self.assertIsNotNone(token)
        sched.release_worker_lease(token)

    def test_lease_released_on_unexpected_error(self):
        with mock.patch.object(batches, "op_file",
                               side_effect=OSError("disk gone")):
            with mock.patch.object(serve, "_require_gate", lambda: None):
                with self.assertRaises(OSError):
                    serve.api_integration_followup(
                        {"op": self.op_id, "request": "sync"})
        token = sched.acquire_worker_lease("probe-after-error")
        self.assertIsNotNone(token)
        sched.release_worker_lease(token)

    def _hook_dir(self):
        out = git("rev-parse", "--git-path", "hooks", cwd=self.wt).strip()
        hooks = (Path(self.wt) / out) if not os.path.isabs(out) else Path(out)
        hooks.mkdir(parents=True, exist_ok=True)
        return hooks

    def test_hooks_suppressed_followup_commits(self):
        # M2.7-SEC F3: app commits never run owner hooks, so a
        # rejecting hook no longer blocks the followup — the commit
        # lands and the hook never runs. (The old hooks-ON premise
        # was the vulnerability; retry-of failed followups is
        # covered by the drift tests.)
        sentinel = Path(self.tmp.name) / "hook-ran.txt"
        hook = self._hook_dir() / "commit-msg"
        hook.write_text("#!/bin/sh\necho ran > %s\necho 'HOOK: reject all for test' >&2\nexit 1\n" % sentinel,
                        encoding="utf-8")
        hook.chmod(0o755)
        try:
            def edit(workdir):
                (workdir / "Podfile.lock").write_text("P: 0.37.1\n",
                                                      encoding="utf-8")
            with mock.patch("openmainty.muse.exec_prompt",
                             side_effect=self.fake_exec(edit=edit)):
                _, fid = batches.request_followup(self.op_id, "sync lock")
                rec = batches.execute_followup(self.op_id, fid)
            entry = [e for e in rec["followups"] if e["fid"] == fid][0]
            self.assertEqual(entry["status"], "done")
            self.assertFalse(sentinel.exists())
            self.assertNotEqual(rec["result"]["head"], self.prep_head)
            # Retry resumes the exact registered after-state.
            with mock.patch("openmainty.muse.exec_prompt",
                             side_effect=self.fake_exec()):
                _, fid2 = batches.request_followup(self.op_id,
                                                    "retry commit")
                rec2 = batches.execute_followup(self.op_id, fid2)
        finally:
            if hook.exists():
                hook.unlink()
        retry = [e for e in rec2["followups"] if e["fid"] == fid2][0]
        self.assertEqual(retry["status"], "done")

    def test_retry_refused_after_drift(self):
        def edit(workdir):
            (workdir / "Podfile.lock").write_text("P: 0.37.1\n",
                                                  encoding="utf-8")
        with mock.patch("openmainty.muse.exec_prompt",
                         side_effect=self.fake_exec(
                             result=(1, "boom", None), edit=edit)):
            _, fid = batches.request_followup(self.op_id, "risky")
            batches.execute_followup(self.op_id, fid)
        (self.wt / "a.txt").write_text("drifted\n", encoding="utf-8")
        with self.assertRaises(batches.IntegrationError):
            batches.request_followup(self.op_id, "retry after drift")

    def test_lease_handoff_clears_reentrant_state(self):
        # A completed async job must not leave a stale reentrant entry
        # on the requesting thread: a distinct later holder still wins.
        with mock.patch.object(serve, "_require_gate", lambda: None):
            with mock.patch("openmainty.muse.exec_prompt",
                             side_effect=self.fake_exec()):
                out = serve.api_integration_followup(
                    {"op": self.op_id, "request": "sync"})
                deadline = time.time() + 60
                while time.time() < deadline:
                    job = serve._JOBS.get(out["job"]["id"])
                    if job is not None and job.get("status") != "running":
                        break
                    time.sleep(0.2)
        self.assertEqual(sched._thread_owners(), [])
        held = threading.Event()
        released = threading.Event()

        def holder():
            token = sched.acquire_worker_lease("later-guard")
            held.set()
            released.wait(timeout=60)
            if token is not None:
                sched.release_worker_lease(token)

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        self.assertTrue(held.wait(timeout=30))
        try:
            with mock.patch.object(serve, "_require_gate", lambda: None):
                with self.assertRaises(serve.ApiError) as ctx:
                    serve.api_integration_followup(
                        {"op": self.op_id, "request": "again"})
            self.assertEqual(ctx.exception.status, 409)
            self.assertIn("later-guard", str(ctx.exception.message))
        finally:
            released.set()
            thread.join(timeout=30)

    def test_endpoint_busy_while_guard_held(self):
        held = threading.Event()
        released = threading.Event()

        def holder():
            token = sched.acquire_worker_lease("external-guard")
            held.set()
            released.wait(timeout=60)
            if token is not None:
                sched.release_worker_lease(token)

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        self.assertTrue(held.wait(timeout=30))
        try:
            with mock.patch.object(serve, "_require_gate", lambda: None):
                with self.assertRaises(serve.ApiError) as ctx:
                    serve.api_integration_followup(
                        {"op": self.op_id, "request": "sync"})
            self.assertEqual(ctx.exception.status, 409)
            self.assertIn("external-guard", str(ctx.exception.message))
            self.assertIn("nothing queued", str(ctx.exception.message))
        finally:
            released.set()
            thread.join(timeout=30)
        self.assertEqual(self.op_record().get("followups"), [])


if __name__ == "__main__":
    unittest.main()
