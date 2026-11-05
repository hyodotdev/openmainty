"""Durable coverage: chat sessions, subscriber gate, transcript, export.

Uses FakeExecutor + an isolated OPENMAINTY_HOME so no subscription quota is
spent. Run with:
  python3 -m unittest discover -s tests
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import chat, pcconfig, repos, runner  # noqa: E402
from openmainty.util import run_cmd  # noqa: E402
try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402

PROFILE = {
    "name": "fakerepo",
    "github": "example/fakerepo",
    "baseBranch": "main",
    "knowledge": [],
    "checks": [{"name": "unit", "cmd": ["make", "unit"], "requires": []}],
    "e2e": [],
    "release": {"notes": "CHANGELOG.md"},
    "announce": {"medium": True, "discord": False, "slack": False},
}


class ChatHomeMixin(unittest.TestCase):
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

    def tearDown(self):
        os.environ.pop("OPENMAINTY_FAKE_QUOTA", None)
        os.environ.pop("META_API_KEY", None)


class WorkdirHookTests(ChatHomeMixin):
    # M2.9b: the chat worktree add never runs repo hooks either.

    def test_ensure_workdir_skips_post_checkout(self):
        import stat as _stat
        import subprocess as _sp
        co = Path(self.tmp.name) / "gco"
        co.mkdir()
        for args in (["init", "-b", "main"], ["config", "user.email", "t@t"],
                     ["config", "user.name", "t"]):
            proc = _sp.run(["git", "-C", str(co)] + args,
                           capture_output=True, text=True, timeout=120)
            self.assertEqual(proc.returncode, 0, proc.stderr[-200:])
        (co / "f.txt").write_text("v1\n", encoding="utf-8")
        proc = _sp.run(["git", "-C", str(co), "add", "."],
                       capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0)
        proc = _sp.run(["git", "-C", str(co), "commit", "-qm", "base"],
                       capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0)
        sentinel = Path(self.tmp.name) / "hook-ran.txt"
        hook = co / ".git" / "hooks" / "post-checkout"
        hook.write_text(
            "#!/bin/sh\n"
            f'echo hooked >> "{sentinel}"\n'
            "echo 'hook must never run' >&2\n"
            "exit 1\n", encoding="utf-8")
        hook.chmod(hook.stat().st_mode | _stat.S_IXUSR)
        session = {"id": "chat-1234abcd"}
        work = chat.ensure_workdir(session, co, "main")
        self.assertTrue(work.is_dir())
        self.assertFalse(sentinel.exists())
        self.assertTrue((work / "f.txt").is_file())


class GateTests(ChatHomeMixin):
    def test_api_key_billing_refused(self):
        os.environ["META_API_KEY"] = "test-not-a-real-key"
        with mock.patch("openmainty.chat.muse.which", return_value="/bin/muse"):
            with self.assertRaises(SystemExit) as ctx:
                chat.require_subscription()
        self.assertIn("subscription-only", str(ctx.exception))

    def test_missing_cli_refused(self):
        with mock.patch("openmainty.chat.muse.which", return_value=None):
            with self.assertRaises(SystemExit) as ctx:
                chat.require_subscription()
        self.assertIn("muse", str(ctx.exception))

    def test_subscriber_path_passes(self):
        with mock.patch("openmainty.chat.muse.which", return_value="/bin/muse"):
            billing = chat.require_subscription()
        self.assertIn("subscription login", billing)


class SessionTests(ChatHomeMixin):
    def make_session(self):
        return chat.create("fakerepo", "1", "fake issue", {"worker": "test"})

    def test_send_persists_user_and_reply(self):
        session = self.make_session()
        turn = chat.send(session, repos.load("fakerepo"), "hello?",
                         runner.FakeExecutor(), self.checkout)
        self.assertEqual(turn["role"], "assistant")
        self.assertIn("fake chat ok", turn["text"])
        transcript = chat.read_transcript(session["id"])
        self.assertEqual([t["role"] for t in transcript], ["user", "assistant"])
        self.assertEqual(transcript[0]["text"], "hello?")
        reloaded = chat.load(session["id"])
        self.assertEqual(reloaded["turns"], 2)
        self.assertIsNone(reloaded["waitReason"])

    def test_second_send_continues_same_session(self):
        session = self.make_session()
        ex = runner.FakeExecutor()
        chat.send(session, repos.load("fakerepo"), "first", ex, self.checkout)
        chat.send(session, repos.load("fakerepo"), "second", ex, self.checkout)
        self.assertEqual(len(chat.read_transcript(session["id"])), 4)
        self.assertIn("first", chat.tail_text(session["id"]))

    def test_quota_wait_records_reason_and_resumes(self):
        session = self.make_session()
        os.environ["OPENMAINTY_FAKE_QUOTA"] = "chat"
        with self.assertRaises(runner.QuotaHit):
            chat.send(session, repos.load("fakerepo"), "hi?",
                      runner.FakeExecutor(), self.checkout)
        session = chat.load(session["id"])
        self.assertEqual(session["waitReason"], "subscription-quota in chat")
        # user turn is kept; no assistant turn yet
        self.assertEqual([t["role"] for t in chat.read_transcript(session["id"])],
                         ["user"])
        del os.environ["OPENMAINTY_FAKE_QUOTA"]
        turn = chat.send(session, repos.load("fakerepo"), "retry",
                         runner.FakeExecutor(), self.checkout)
        self.assertEqual(turn["role"], "assistant")
        self.assertIsNone(chat.load(session["id"])["waitReason"])

    def test_usage_honesty_no_measured_tokens(self):
        session = self.make_session()
        turn = chat.send(session, repos.load("fakerepo"), "hi?",
                         runner.FakeExecutor(), self.checkout)
        usage = turn.get("usage") or {}
        if "tokens" in usage:
            self.assertIsNone(usage["tokens"])
        self.assertEqual(usage.get("text_source"), "passthrough")

    def test_export_markdown_contains_turns(self):
        session = self.make_session()
        chat.send(session, repos.load("fakerepo"), "describe the bug",
                  runner.FakeExecutor(), self.checkout)
        md = chat.export_markdown(chat.load(session["id"]))
        self.assertIn(session["id"], md)
        self.assertIn("## user", md)
        self.assertIn("describe the bug", md)
        self.assertIn("## assistant", md)

    def test_list_sessions_newest_first(self):
        first = self.make_session()
        first["created_at"] = "2020-01-01T00:00:00+00:00"
        chat.save(first)
        second = chat.create("fakerepo", "2", "other", {})
        ids = [s["id"] for s in chat.list_sessions()]
        self.assertEqual(ids, [second["id"], first["id"]])

    def test_workdir_copy_fallback_never_touches_checkout(self):
        session = self.make_session()
        work = chat.ensure_workdir(session, self.checkout, "main")
        self.assertTrue((work / "README.md").is_file())
        self.assertEqual(chat.load(session["id"])["workdir_mode"], "copy")
        (work / "scratch.txt").write_text("x")
        self.assertFalse((self.checkout / "scratch.txt").exists())

    def test_delete_unregisters_worktree(self):
        run_cmd(["git", "-C", str(self.checkout), "init"])
        run_cmd(["git", "-C", str(self.checkout), "add", "."])
        run_cmd(["git", "-C", str(self.checkout), "-c", "user.email=t@t",
                 "-c", "user.name=t", "commit", "-m", "init"])
        run_cmd(["git", "-C", str(self.checkout), "branch", "-M", "main"])
        session = self.make_session()
        work = chat.ensure_workdir(session, self.checkout, "main")
        self.assertEqual(chat.load(session["id"])["workdir_mode"], "worktree")
        chat.delete(session["id"], self.checkout)
        self.assertFalse(chat.session_dir(session["id"]).exists())
        _, out, _ = run_cmd(["git", "-C", str(self.checkout), "worktree", "list"])
        self.assertNotIn(str(work), out)

    def test_delete_unknown_session(self):
        with self.assertRaises(SystemExit):
            chat.delete("chat-deadbeef")

    def test_extract_plain_text_passthrough(self):
        text, source = chat.assistant_text("plain reply", {}, Path(self.tmp.name))
        self.assertEqual((text, source), ("plain reply", "passthrough"))

    def test_extract_empty_reports_honestly(self):
        text, source = chat.assistant_text("", {}, Path(self.tmp.name))
        self.assertEqual(text, "(empty worker output)")
        self.assertEqual(source, "raw")


class SyncRunSessionTests(ChatHomeMixin):
    def make_run(self):
        from openmainty import store
        state = store.create("fakerepo", "7", "thing", "base0", {})
        store.mark(state, "triage", "done", note="actionable")
        store.mark(state, "fix", "failed", note="boom")
        state["waitReason"] = "question: which shape?"
        store.save(state)
        return state

    def test_sync_creates_session_with_turns(self):
        from openmainty import store
        state = self.make_run()
        before = [t for s in chat.list_sessions(limit=500)
                  for t in chat.read_transcript(s["id"])]
        session = chat.sync_run_session(store.load(state["id"]))
        self.assertEqual(session["run_id"], state["id"])
        self.assertEqual(session["repo"], "fakerepo")
        self.assertNotIn("#7 ", session["title"])
        after = [t for s in chat.list_sessions(limit=500)
                 for t in chat.read_transcript(s["id"])]
        new = after[len(before):]
        self.assertEqual(len(new), 2)
        self.assertIn("유지보수 시작", new[0]["text"])
        self.assertEqual(new[1]["role"], "assistant")
        self.assertIn("분류", new[1]["text"])
        self.assertIn("수정", new[1]["text"])
        self.assertIn("질문", new[1]["text"])

    def test_sync_is_idempotent(self):
        from openmainty import store
        state = self.make_run()
        chat.sync_run_session(store.load(state["id"]))
        n1 = len(chat.read_transcript(chat.session_for_run(state["id"])["id"]))
        chat.sync_run_session(store.load(state["id"]))
        n2 = len(chat.read_transcript(chat.session_for_run(state["id"])["id"]))
        self.assertEqual((n1, n2), (2, 2))

    def test_sync_notes_are_humanized(self):
        self.assertEqual(chat._note_ko("muse worker exit 0 with expected markers"), "정상 완료")
        self.assertEqual(chat._note_ko("reviewer found no VALID findings"), "지적 없음")
        self.assertEqual(chat._note_ko("1 check(s) passed"), "검사 통과")
        self.assertEqual(chat._note_ko("drafts written (dry-run only; nothing posted)"),
                         "초안 작성됨(미게시)")
        self.assertEqual(chat._note_ko("worker failed (exit 124)"), "작업자 실패(exit 124)")
        self.assertEqual(chat._note_ko("asked: stop or pause?"), "질문: stop or pause?")
        self.assertEqual(chat._note_ko("something custom"), "something custom")

    def test_execute_syncs_session(self):
        from openmainty import store
        state = store.create("fakerepo", "8", "t2", "base0", {})
        self.assertTrue(runner.execute(state, repos.load("fakerepo"),
                                       runner.FakeExecutor(), self.checkout,
                                       only=["triage"]))
        session = chat.session_for_run(state["id"])
        self.assertIsNotNone(session)
        turns = chat.read_transcript(session["id"])
        self.assertTrue(any("분류" in t["text"] for t in turns))

    def test_fix_line_names_changed_files(self):
        from openmainty import store
        state = store.create("fakerepo", "9", "t3", "base0", {})
        store.mark(state, "fix", "done", note="1 file changed")
        store.run_dir(state["id"]).mkdir(parents=True, exist_ok=True)
        (store.run_dir(state["id"]) / "fix.diffstat.txt").write_text(
            " src/a.ts | 3 ++-\n 1 file changed, 2 insertions(+)\n",
            encoding="utf-8")
        session = chat.sync_run_session(store.load(state["id"]))
        turns = chat.read_transcript(session["id"])
        self.assertTrue(any("src/a.ts" in t["text"] for t in turns))

    def test_all_done_appends_closing_turn(self):
        from openmainty import store
        state = store.create("fakerepo", "10", "t4", "base0", {})
        for step in store.STEP_ORDER:
            store.mark(state, step, "done", note="ok")
        session = chat.sync_run_session(store.load(state["id"]))
        turns = chat.read_transcript(session["id"])
        self.assertIn("유지보수 완료", turns[-1]["text"])


class RunSummaryTests(ChatHomeMixin):
    def make_done_run(self, issue="7", refs="#9"):
        from openmainty import store
        state = store.create("fakerepo", issue, "thing", "abc123def456789", {})
        for step in store.STEP_ORDER:
            store.mark(state, step, "done", note="1 check(s) passed")
        rdir = store.run_dir(state["id"])
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / "fix.diffstat.txt").write_text(
            " src/a.ts | 3 ++-\n src/b.ts | 1 +\n 2 files changed\n", encoding="utf-8")
        (rdir / "triage.log.txt").write_text(f"DUPLICATE of {refs}\n", encoding="utf-8")
        (rdir / "pr.md").write_text("Fixes #8.\n", encoding="utf-8")
        state["artifacts"] = {"pr": "pr.md"}
        store.save(state)
        return store.load(state["id"])

    def test_full_card(self):
        peer = chat.create("fakerepo", "8", "peer issue", {})
        session = chat.sync_run_session(self.make_done_run())
        card = chat.run_summary(session)
        self.assertEqual(card["status"], "done")
        self.assertEqual(card["issue"], "7")
        self.assertEqual(card["files"], ["src/a.ts", "src/b.ts"])
        self.assertIn("2 files changed", card["files_total"])
        self.assertEqual(card["drafts"], [{"name": "pr.md", "kind": "PR 본문"}])
        self.assertEqual(card["checks"], "검사 통과")
        self.assertEqual(card["github"], "example/fakerepo")
        self.assertEqual(card["base"], "abc123def456")
        got = {r["issue"]: r for r in card["related"]}
        self.assertEqual(set(got), {"9", "8"})
        self.assertEqual(got["9"]["via"], "분류")
        self.assertIsNone(got["9"]["session_id"])
        self.assertEqual(got["8"]["via"], "초안")
        self.assertEqual(got["8"]["session_id"], peer["id"])
        self.assertEqual(card["question"], "")

    def test_waiting_and_siblings(self):
        from openmainty import store
        state = self.make_done_run(issue="11", refs="#11")
        state["waitReason"] = "question: which shape?"
        store.save(state)
        first = chat.sync_run_session(store.load(state["id"]))
        second = chat.create("fakerepo", "11", "dup", {})
        second["run_id"] = state["id"]
        chat.save(second)
        card = chat.run_summary(chat.load(first["id"]))
        self.assertEqual(card["status"], "waiting")
        self.assertEqual(card["question"], "which shape?")
        self.assertNotIn("11", [r["issue"] for r in card["related"]])
        self.assertEqual([s["id"] for s in card["siblings"]], [second["id"]])

    def test_no_run_no_card(self):
        session = chat.create("fakerepo", "1", "plain", {})
        self.assertIsNone(chat.run_summary(session))
        self.assertIsNone(chat.session_card(session))


class ReplySessionTests(ChatHomeMixin):
    def write_draft(self, name="xx-7.md", text="See #8 for root cause.\n"):
        d = Path(os.environ["OPENMAINTY_HOME"]) / "drafts"
        d.mkdir(exist_ok=True)
        (d / name).write_text(text, encoding="utf-8")
        return name

    def test_sync_creates_linked_card(self):
        peer = chat.create("fakerepo", "8", "peer", {})
        rel = self.write_draft()
        first = chat.sync_reply_session("fakerepo", "7", "reply title", rel)
        again = chat.sync_reply_session("fakerepo", "7", "reply title", rel)
        self.assertEqual(first["id"], again["id"])
        turns = chat.read_transcript(first["id"])
        self.assertEqual(len(turns), 1)
        self.assertIn("미게시", turns[0]["text"])
        card = chat.session_card(chat.load(first["id"]))
        self.assertEqual(card["status"], "draft")
        self.assertEqual(card["file_run"], "_drafts")
        self.assertEqual(card["drafts"], [{"name": rel, "kind": "답글 초안"}])
        self.assertEqual(len(card["related"]), 1)
        self.assertEqual(card["related"][0]["issue"], "8")
        self.assertEqual(card["related"][0]["session_id"], peer["id"])

    def test_sync_rewrites_on_change(self):
        rel = self.write_draft()
        first = chat.sync_reply_session("fakerepo", "7", "t", rel)
        self.write_draft(text="Updated #9.\n")
        second = chat.sync_reply_session("fakerepo", "7", "t", rel)
        self.assertEqual(first["id"], second["id"])
        turns = chat.read_transcript(first["id"])
        self.assertEqual(len(turns), 1)
        self.assertIn("Updated #9.", turns[0]["text"])

    def test_drafts_file_scope(self):
        from openmainty import serve
        rel = self.write_draft()
        dest = serve.safe_run_file("_drafts", rel)
        self.assertTrue(dest and dest.is_file())
        self.assertIsNone(serve.safe_run_file("_drafts", "../x.md"))
        self.assertIsNone(serve.safe_run_file("_drafts", "missing.md"))


class DeleteContainmentTests(ChatHomeMixin):
    # F1: traversal / absolute / mismatched ids are refused, and a
    # crafted stored id can never rmtree outside the sessions dir.
    def test_traversal_id_refused(self):
        with self.assertRaises(SystemExit) as ctx:
            chat.delete("../outside", None)
        self.assertIn("invalid chat session id", str(ctx.exception))

    def test_absolute_id_refused(self):
        with self.assertRaises(SystemExit):
            chat.load("/tmp/om66-f1-abs")
        with self.assertRaises(SystemExit):
            chat.delete("/tmp/om66-f1-abs", None)

    def test_mismatched_stored_id_refused(self):
        import json as _json
        session = chat.create("fakerepo", "1", "t", {})
        sid = session["id"]
        victim = Path(self.tmp.name) / "victim.txt"
        victim.write_text("owner data\n", encoding="utf-8")
        # A crafted record whose stored id is an absolute path: the
        # old code joined it straight into the rmtree target. Write
        # it directly — save() itself validates and would refuse.
        session["id"] = str(victim)
        path = chat.session_dir(sid) / "session.json"
        path.write_text(_json.dumps(session), encoding="utf-8")
        with self.assertRaises(SystemExit):
            chat.delete(sid, None)
        self.assertTrue(victim.is_file())
        self.assertEqual(victim.read_text(encoding="utf-8"),
                         "owner data\n")

    def test_legit_delete_still_works(self):
        session = chat.create("fakerepo", "1", "t", {})
        sid = session["id"]
        self.assertTrue(chat.session_dir(sid).is_dir())
        chat.delete(sid, None)
        self.assertFalse(chat.session_dir(sid).exists())


if __name__ == "__main__":
    unittest.main()
