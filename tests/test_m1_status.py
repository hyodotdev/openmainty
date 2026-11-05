"""M1 glance snapshot + digest (status page foundation).

Covers batches.m1_* pure builders (verdict ladder, suggestions,
scan-collapse, sentences, ages), the snapshot refresh on a fixture
home (questions, release dates, story, digest files — never Desktop),
and the /api/summary + /api/digest endpoints (unknown envelopes,
budgets). Dashboard route wiring is guarded by structural asserts;
visible behavior is verified in the real browser during self-use.

Py3.9 portable.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import batches  # noqa: E402
from openmainty.util import write_json  # noqa: E402
try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402


def git(*args, cwd, env=None):
    import os as _os
    full = dict(_os.environ)
    if env:
        full.update(env)
    proc = subprocess.run(["git", "-C", str(cwd)] + list(args),
                          capture_output=True, text=True, timeout=120,
                          env=full)
    if proc.returncode != 0:
        raise AssertionError(
            "git %s failed: %s" % (" ".join(args), proc.stderr[-300:]))
    return proc.stdout


class VerdictTests(unittest.TestCase):
    def v(self, **kw):
        base = {"ready": 0, "blocked": 0, "questions": 0,
                "unreadable": [], "waiting_review": 0,
                "pending_release": 0, "days_since_release": None}
        base.update(kw)
        return batches.m1_verdict(**base)

    def test_blocked_first(self):
        r = self.v(ready=5, blocked=1, questions=2,
                   unreadable=["x"], waiting_review=1)
        self.assertEqual((r["verdict"], r["rule"]), ("blocked", "blocked-items"))
        self.assertTrue(r["line"].startswith("막힘 —"))

    def test_unreadable_beats_attention(self):
        r = self.v(ready=3, questions=1, unreadable=["묶음 b (OSError)"])
        self.assertEqual((r["verdict"], r["rule"]),
                         ("unknown", "unreadable-inputs"))
        self.assertIn("판단 보류", r["line"])
        # 정상 is forbidden here even though ready > 0.
        self.assertNotIn("정상", r["line"])

    def test_questions_mean_attention(self):
        # Today's real shape: 32 ready, 0 blocked, 6 questions.
        r = self.v(ready=32, questions=6)
        self.assertEqual((r["verdict"], r["rule"]),
                         ("attention", "open-questions"))
        self.assertTrue(r["line"].startswith("주의 —"))
        self.assertIn("질문 6건", r["line"])

    def test_stale_release(self):
        r = self.v(ready=32, pending_release=32, days_since_release=44)
        self.assertEqual((r["verdict"], r["rule"]),
                         ("attention", "stale-release"))

    def test_waiting_review(self):
        r = self.v(ready=0, waiting_review=2)
        self.assertEqual(r["verdict"], "attention")

    def test_normal_ready(self):
        r = self.v(ready=2)
        self.assertEqual((r["verdict"], r["rule"]),
                         ("normal", "ready-and-clear"))
        self.assertIn("질문 0건", r["line"])

    def test_normal_empty(self):
        r = self.v()
        self.assertEqual((r["verdict"], r["rule"]),
                         ("normal", "nothing-pending"))


class SuggestTests(unittest.TestCase):
    def test_explicit_choice_suggests_newest(self):
        s = batches.m1_suggest("어느 버전으로 올릴까요? 1.2.3 또는 2.0.1?")
        self.assertEqual(s["text"], "2.0.1")
        self.assertIn("보내기 전", s["basis"])

    def test_yes_no_gets_no_suggestion(self):
        self.assertEqual(batches.m1_suggest("계속할까요?")["text"], "")
        self.assertEqual(batches.m1_suggest("")["text"], "")

    def test_reporter_repro_request_gets_no_suggestion(self):
        # Real run-07a2b957: a repro-info request that merely mentions
        # versions (0.2.10, 0.2.16) must NOT suggest "0.2.16".
        q = ("exact URI string passed to startPlayer + how it was built, "
             "awaited promise result/logcat output, and retest on latest "
             "version (reporter is on 0.2.10; android.resource:// support "
             "landed in v0.2.16)")
        self.assertEqual(batches.m1_suggest(q)["text"], "")


class CollapseTests(unittest.TestCase):
    def test_equal_runs_fold(self):
        scans = [((68, 43, 14), "2026-10-20T13:44:00+00:00", False, ""),
                 ((68, 43, 14), "2026-10-20T13:29:00+00:00", False, ""),
                 ((68, 43, 14), "2026-10-20T13:14:00+00:00", False, "")]
        self.assertEqual(batches.m1_collapse_scans(scans),
                         ["10/20 22:14~22:44, 3회 점검, 변화 없음"])

    def test_folded_span_labels_order(self):
        # S10: two folded runs around a finding stay distinguishable
        # by their spans (oldest~newest); cross-day spans print both.
        scans = [((68, 43, 14), "2026-10-20T13:44:00+00:00", False, ""),
                 ((68, 43, 14), "2026-10-20T13:14:00+00:00", False, ""),
                 ((68, 44, 14), "2026-10-20T12:44:00+00:00", True,
                  "새 업데이트 감지: foo 1.0"),
                 ((68, 43, 14), "2026-10-19T13:44:00+00:00", False, ""),
                 ((68, 43, 14), "2026-10-19T13:14:00+00:00", False, "")]
        self.assertEqual(
            batches.m1_collapse_scans(scans),
            ["10/20 22:14~22:44, 2회 점검, 변화 없음",
             "새 업데이트 감지: foo 1.0",
             "10/19 22:14~22:44, 2회 점검, 변화 없음"])

    def test_finding_names_what(self):
        found = "새 업데이트 감지: @babel/core 8.0.7 외 9건"
        scans = [((68, 43, 14), "2026-10-20T13:44:00+00:00", False, ""),
                 ((68, 44, 14), "2026-10-20T13:29:00+00:00", True,
                  found)]
        out = batches.m1_collapse_scans(scans)
        self.assertEqual(len(out), 2)
        self.assertIn(found, out)

    def test_single_scan_not_folded(self):
        out = batches.m1_collapse_scans(
            [((1, 0, 0), "2026-10-20T13:44:00+00:00", False, "")])
        self.assertEqual(out, ["점검함 (10/20 22:44)"])

    def test_unparseable_breaks(self):
        scans = [(None, "2026-10-20T13:44:00+00:00", False, ""),
                 ((68, 43, 14), "2026-10-20T13:29:00+00:00", False, "")]
        out = batches.m1_collapse_scans(scans)
        self.assertEqual(len(out), 2)
        self.assertTrue(any("읽지 못함" in t for t in out))

    def test_real_shaped_repeats_fold(self):
        # Real nitro shape: identical title+detail every tick folds.
        detail = ("@babel/core->8.0.7; @commitlint/cli->21.2.4; "
                  "@eslint/js->10.0.1")
        scans = [((68, 43, 14), "2026-10-20T14:15:56+00:00", False, ""),
                 ((68, 43, 14), "2026-10-20T13:45:29+00:00", False, ""),
                 ((68, 43, 14), "2026-10-20T13:15:07+00:00", False, "")]
        self.assertEqual(batches.m1_collapse_scans(scans),
                         ["10/20 22:15~23:15, 3회 점검, 변화 없음"])
        self.assertEqual(
            batches._m1_scan_found(detail),
            "새 업데이트 감지: @babel/core 8.0.7 외 2건")
        self.assertEqual(batches._m1_scan_found(""), "")


class NeedLineTests(unittest.TestCase):
    def test_question_text_kept(self):
        self.assertEqual(
            batches.m1_need_line({"question": "어느 버전으로?", "age_text":
                                  "3일째 대기"}),
            "- 질문(3일째 대기): 어느 버전으로?")

    def test_empty_question_falls_back_to_title_then_ref(self):
        # S10: the live digest printed "- 질문(3일째 대기): " twice.
        self.assertEqual(
            batches.m1_need_line({"question": "", "title": "#431 분석",
                                  "ref": "run-x", "age_text": "3일째 대기"}),
            "- 질문(3일째 대기): #431 분석")
        self.assertEqual(
            batches.m1_need_line({"ref": "run-x", "age_text": "오늘"}),
            "- 질문(오늘): run-x")

    def test_nothing_names_it_drops_line(self):
        self.assertEqual(batches.m1_need_line({}), "")
        self.assertEqual(batches.m1_need_line({"question": ""}), "")
        self.assertEqual(batches.m1_need_line("x"), "")

    def test_n1_reshaped_line_and_gate(self):
        # N1: the reshaped note renders as owner work, shows only
        # while its op is newest, and never counts as a question.
        import time
        now = time.time()
        st = {"m2_rollup_reshaped": {"lane": "next",
                                    "op": "rollup-next-old",
                                    "at": "2026-10-21T00:00:00+00:00"}}
        item = batches.reshaped_note_item(
            st, {"next": {"op": "rollup-next-old"}}, now)
        self.assertIsNotNone(item)
        self.assertEqual(item["kind"], "rollup-reshaped")
        self.assertEqual(
            batches.m1_need_line(item),
            "- 확인 필요(%s): next 레인 구성이 바뀜 — 이전 합치기 "
            "rollup-next-old와 다름, 사람이 확인" % item["age_text"])
        # A newer rollup hides it without touching the stash.
        self.assertIsNone(batches.reshaped_note_item(
            st, {"next": {"op": "rollup-next-new"}}, now))
        self.assertIsNone(batches.reshaped_note_item({}, {}, now))
        self.assertIsNone(batches.reshaped_note_item(
            {"m2_rollup_reshaped": {"lane": "next"}}, {}, now))

    def test_l1_issue_review_labeled_not_question(self):
        # L1: issue-review lines match the card count wording.
        self.assertEqual(
            batches.m1_need_line({"kind": "issue-review",
                                  "title": "#431 분석",
                                  "age_text": "3일째 대기"}),
            "- 이슈 검토(3일째 대기): #431 분석")
        # A question item without the kind keeps the old wording.
        self.assertEqual(
            batches.m1_need_line({"kind": "question",
                                  "question": "어느 버전으로?",
                                  "age_text": "오늘"}),
            "- 질문(오늘): 어느 버전으로?")


class SentenceTests(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(batches.m1_event_sentence("scan", "scan: 1"), "")
        self.assertIn("끝냄", batches.m1_event_sentence("run-done", "foo 1.0"))
        self.assertIn("실패", batches.m1_event_sentence("run-error", "x"))
        self.assertEqual(batches.m1_event_sentence("unknown-kind", "x"), "")

    def test_age(self):
        now = time.time()
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3 * 86400 - 100))
        self.assertEqual(batches.m1_question_age_days({"created_at": old}, now), 3)
        self.assertIsNone(batches.m1_question_age_days({}, now))


OLD_COMMIT = "2026-09-06T12:00:00+00:00"


class RefreshFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        use_temp_home(self, str(self.root / "home"))
        from openmainty import events, paths, repos, store  # noqa: E402
        self.events = events
        self.paths = paths
        (paths.repos_dir()).mkdir(parents=True, exist_ok=True)
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        git("init", "-b", "main", cwd=self.checkout)
        git("config", "user.email", "test@localhost", cwd=self.checkout)
        git("config", "user.name", "test", cwd=self.checkout)
        (self.checkout / "f.txt").write_text("x\n", encoding="utf-8")
        git("add", "-A", cwd=self.checkout)
        git("commit", "-qm", "old release", cwd=self.checkout,
            env={"GIT_AUTHOR_DATE": OLD_COMMIT,
                 "GIT_COMMITTER_DATE": OLD_COMMIT})
        write_json(paths.repos_dir() / "fakerepo.json", {
            "name": "fakerepo", "github": "o/r", "baseBranch": "main",
            "checkout": str(self.checkout)})
        state = store.create("fakerepo", "dep-foo-1.2.3",
                             "chore(deps): foo", "c0" * 10, {})
        state["id"] = "run-testm1q1"
        three = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                              time.gmtime(time.time() - 3 * 86400 - 50))
        state["created_at"] = three
        state["waitReason"] = ("question: 어느 버전으로 올릴까요? "
                               "1.2.3 또는 2.0.1?")
        state["dep"] = "foo"
        state["target"] = "2.0.1"
        store.save(state)
        for _ in range(3):
            events.append("fakerepo", "scan", "",
                          "scan: 68 deps, 43 drift, 14 lock-refresh "
                          "(+0 queued, 0 refreshed)", "")
        events.append("fakerepo", "run-done", "run-testm1q1",
                      "foo 2.0.1 검증", "proof")

    def test_refresh_row(self):
        snap = batches.refresh_summary_snapshot("test")
        self.assertEqual(snap["writer"], "test")
        self.assertTrue(snap["at"])
        self.assertEqual(len(snap["repos"]), 1)
        row = snap["repos"][0]
        self.assertEqual(row["name"], "fakerepo")
        # One open question -> attention, never 정상.
        self.assertEqual(row["verdict"], "attention")
        self.assertEqual(row["verdict_rule"], "open-questions")
        self.assertIn("질문 1건", row["verdict_line"])
        need = row["need_you"]
        self.assertEqual(len(need), 1)
        self.assertEqual(need[0]["ref"], "run-testm1q1")
        self.assertEqual(need[0]["age_days"], 3)
        self.assertEqual(need[0]["age_text"], "3일째 대기")
        self.assertEqual(need[0]["suggested"], "2.0.1")
        self.assertIn("보내기 전", need[0]["suggested_basis"])
        # Release 2026-09-06 is old; story folds the 3 scans.
        rel = row["pending_release"]
        self.assertEqual(rel["last_release"], "2026-09-06")
        self.assertGreaterEqual(rel["days_since"], 30)
        texts = row["story"]
        folded = [t for t in texts if "변화 없음" in t]
        self.assertEqual(len(folded), 1)
        self.assertIn("3회 점검", folded[0])
        self.assertTrue(any("끝냄" in t for t in texts))
        # Snapshot file round-trips within budget.
        raw = batches.summary_snapshot_path().read_bytes()
        self.assertLessEqual(len(raw), 30 * 1024)
        again = batches.refresh_summary_snapshot("test")
        self.assertEqual(again["repos"][0]["verdict"], "attention")

    def test_digest_never_desktop(self):
        data = batches.write_digest("2026-10-20")
        self.assertEqual(data["date"], "2026-10-20")
        md = batches.digest_md_file("2026-10-20").read_text(
            encoding="utf-8")
        self.assertIn("# 2026-10-20 하루 요약", md)
        self.assertIn("주의 —", md)
        self.assertIn("3일째 대기", md)
        # Digest must be byte-consistent with the JSON card.
        self.assertIn(data["repos"][0]["verdict_line"], md)
        # Nothing outside the app home may have been created here.
        self.assertFalse((self.root / "Desktop").exists())
        self.assertEqual(batches.read_digest("2026-10-20")["date"],
                         "2026-10-20")
        self.assertIsNone(batches.read_digest("1999-01-01"))
        self.assertIsNone(batches.read_digest("../evil"))


class SystemExitContainmentTests(RefreshFixture):
    """M25a-F1: a CLI-style SystemExit from deep code degrades one
    glance row, never the whole refresh."""

    def test_systemexit_in_counts_still_renders_row(self):
        from openmainty import m2
        real = m2.fastcheck_count_line
        self.addCleanup(setattr, m2, "fastcheck_count_line", real)

        def boom(repo, **kwargs):
            raise SystemExit("error: simulated deep exit")
        m2.fastcheck_count_line = boom
        snap = batches.refresh_summary_snapshot("test")
        self.assertEqual(len(snap["repos"]), 1)
        row = snap["repos"][0]
        self.assertEqual(row["name"], "fakerepo")
        self.assertEqual(row["verdict"], "attention")


class TitleTranslateTests(unittest.TestCase):
    def test_leading_words(self):
        self.assertEqual(
            batches._m1_ko_title("superseded: foo 1.0"), "대체됨: foo 1.0")
        self.assertEqual(batches._m1_ko_title("foo 1.0"), "foo 1.0")
        self.assertEqual(batches._m1_ko_title("unknownword: x"),
                         "unknownword: x")

    def test_cap(self):
        long = "가" * 200
        capped = batches._m1_story_cap(long)
        self.assertTrue(capped.endswith("… (자세히)"))
        self.assertLessEqual(len(capped), 90)
        self.assertEqual(batches._m1_story_cap("짧음"), "짧음")

    def test_local_day_shape(self):
        import re
        self.assertTrue(re.match(r"^\d{4}-\d{2}-\d{2}$",
                                 batches.m1_local_day()))
        self.assertTrue(re.match(r"^\d{4}-\d{2}-\d{2}$",
                                 batches.m1_local_day(-1)))


class DigestDayTests(RefreshFixture):
    def test_provisional_title_and_marker(self):
        snap = batches.refresh_summary_snapshot("test")
        mid = batches.write_digest("2026-10-20", provisional=True,
                                   snap=snap)
        self.assertTrue(mid["provisional"])
        self.assertTrue(mid["title"].startswith("오늘 지금까지"))
        md = batches.digest_md_file("2026-10-20").read_text(
            encoding="utf-8")
        self.assertIn("# 오늘 지금까지", md)
        # A mid-day write must not mark the day finished.
        day_file = batches.summary_digest_day_path()
        if day_file.exists():
            self.assertNotEqual(day_file.read_text(
                encoding="utf-8").strip(), "2026-10-20")
        final = batches.write_digest("2026-10-20", snap=snap)
        self.assertFalse(final["provisional"])
        self.assertEqual(final["title"], "2026-10-20 하루 요약")
        self.assertEqual(day_file.read_text(
            encoding="utf-8").strip(), "2026-10-20")


class ReadyDriftTests(RefreshFixture):
    """D1: no eval cache — a live drift changes the verdict even when no
    batch file moved."""

    def _write_ready_batch(self):
        from openmainty import runner, store
        from openmainty.util import utcnow
        c0 = git("rev-parse", "HEAD", cwd=self.checkout).strip()
        git("branch", "-f", "next", c0, cwd=self.checkout)
        run_wt = self.root / "run-wt"
        git("worktree", "add", "--detach", str(run_wt), c0,
            cwd=self.checkout)
        csnap = runner.source_snapshot(run_wt)
        integ_wt = self.root / "integ-wt"
        git("worktree", "add", "--detach", str(integ_wt), c0,
            cwd=self.checkout)
        snap = runner.source_snapshot(integ_wt)
        state = store.create("fakerepo", "dep-foo-1.2.3",
                             "chore(deps): foo", c0, {})
        state["id"] = "run-testm1ready"
        state["dep"] = "foo"
        state["target"] = "1.2.3"
        state["lane"] = "next"
        state["workCommit"] = c0
        state["workTreeFingerprint"] = csnap["fingerprint"]
        state["verifiedCommits"] = {"review": c0, "e2e": c0}
        state["verifiedFingerprints"] = {"review": csnap["fingerprint"],
                                         "e2e": csnap["fingerprint"]}
        state["steps"] = {"review": {"status": "done", "exit_code": 0},
                          "e2e": {"status": "done", "exit_code": 0}}
        store.save(state)
        batches.batches_dir().mkdir(parents=True, exist_ok=True)
        ev_dir = batches.evidence_dir("test-m1-ready")
        ev_dir.mkdir(parents=True, exist_ok=True)
        (ev_dir / "proof.txt").write_text("proof\n", encoding="utf-8")
        ev_sha = batches.sha256_file(ev_dir / "proof.txt")
        write_json(batches.batches_dir() / "test-m1-ready.json", {
            "schema": 3, "id": "test-m1-ready", "repo": "fakerepo",
            "target_branch": "next", "purpose": "test ready",
            "updated_at": utcnow(),
            "primaries": [{
                "dep": "foo", "target": "1.2.3", "lane": "next",
                "run_id": "run-testm1ready",
                "review": {"verdict": "accept"},
                "required_gates": ["unit"],
                "source": {"worktree": str(run_wt), "head": c0,
                           "fingerprint": csnap["fingerprint"]}}],
            "inherited": [],
            "origin": {"checkout": str(self.checkout),
                       "base_branch": "next", "base_head": c0},
            "integrations": [{
                "lane": "next", "worktree": str(integ_wt),
                "head": c0, "fingerprint": snap["fingerprint"],
                "status": "integration-testing",
                "gates": [{
                    "key": "unit", "status": "pass",
                    "inputs": {"head": c0,
                               "fingerprint": snap["fingerprint"]},
                    "evidence": [{
                        "label": "unit proof", "kind": "file",
                        "stored": True, "stored_path": "proof.txt",
                        "sha256": ev_sha, "bytes": 6}]}]}],
            "policy": {"gates": {"unit": {"label": "unit"}},
                       "required_gates": []}})
        return integ_wt

    def test_no_stale_cache(self):
        self._write_ready_batch()
        # A leftover evals.json claiming not-ready must be ignored:
        # there is no cache anymore, every refresh evaluates fresh.
        from openmainty import paths as _paths
        (_paths.home() / "summary").mkdir(parents=True, exist_ok=True)
        write_json(_paths.home() / "summary" / "evals.json", {
            "test-m1-ready": {"sha": "stale",
                              "evaluation": {"results": [{"ready": False,
                                                          "diag": {}}]}}})
        snap = batches.refresh_summary_snapshot("test")
        row = snap["repos"][0]
        self.assertEqual(row["pending_release"]["count"], 1)

    def test_live_drift_changes_verdict(self):
        integ_wt = self._write_ready_batch()
        before = batches.sha256_file(
            batches.batches_dir() / "test-m1-ready.json")
        first = batches.refresh_summary_snapshot("test")
        self.assertEqual(first["repos"][0]["pending_release"]["count"], 1)
        git("commit", "-q", "--allow-empty", "-m", "drift",
            cwd=integ_wt)
        second = batches.refresh_summary_snapshot("test")
        self.assertEqual(second["repos"][0]["pending_release"]["count"], 0)
        after = batches.sha256_file(
            batches.batches_dir() / "test-m1-ready.json")
        self.assertEqual(before, after)


class WaitingReviewTests(RefreshFixture):
    """D4: 검토 대기 counts reviewed-but-unbatched queue items."""

    def test_unbatched_count_in_line(self):
        from openmainty import sched
        sched._file("fakerepo").parent.mkdir(parents=True, exist_ok=True)
        write_json(sched._file("fakerepo"), {
            "repo": "fakerepo", "queue": [
                {"status": "waiting-review", "repo": "fakerepo",
                 "dep": "aaa", "target": "1.0.0", "lane": "next",
                 "run_id": "run-a", "branch": "b"},
                {"status": "waiting-review", "repo": "fakerepo",
                 "dep": "bbb", "target": "2.0.0", "lane": "next",
                 "run_id": "run-b", "branch": "b"}]})
        snap = batches.refresh_summary_snapshot("test")
        line = snap["repos"][0]["verdict_line"]
        self.assertIn("검토 대기 2건", line)


class IssueWaitingTests(RefreshFixture):
    """D4b: issue-triage items waiting for review count as 이슈 검토 대기,
    excluding runs already listed as questions (never counted twice)."""

    def _queue(self, items):
        from openmainty import sched
        sched._file("fakerepo").parent.mkdir(parents=True, exist_ok=True)
        write_json(sched._file("fakerepo"),
                   {"repo": "fakerepo", "queue": items})

    def _item(self, run, title, dep=None):
        it = {"kind": "issue-triage", "repo": "fakerepo",
              "issue": "431", "title": title, "status": "waiting-review",
              "run_id": run, "created_at": "2026-10-17T17:10:55+00:00"}
        if dep:
            it["dep"] = dep
        return it

    def test_issue_only_run_counted(self):
        # run-testm1q1 is already a question; run-issue1 is issue-only.
        self._queue([
            self._item("run-testm1q1",
                       "Migrate to material_ui/cupertino_ui (4.0.0)"),
            self._item("run-issue1",
                       "Migrate to material_ui/cupertino_ui (4.0.0)")])
        snap = batches.refresh_summary_snapshot("test")
        row = snap["repos"][0]
        self.assertIn("이슈 검토 대기 1건", row["verdict_line"])
        kinds = [n["kind"] for n in row["need_you"]]
        self.assertEqual(kinds.count("question"), 1)
        self.assertEqual(kinds.count("issue-review"), 1)
        issue = [n for n in row["need_you"]
                 if n["kind"] == "issue-review"][0]
        self.assertEqual(issue["ref"], "run-issue1")
        self.assertIn("Migrate", issue["title"])
        self.assertEqual(issue["suggested"], "")
        self.assertIn("답변/종료 결정", issue["why"])

    def test_dep_items_not_double_counted(self):
        self._queue([self._item("run-dep1", "dep bump", dep="foo")])
        snap = batches.refresh_summary_snapshot("test")
        kinds = [n["kind"] for n in snap["repos"][0]["need_you"]]
        self.assertNotIn("issue-review", kinds)

    def test_issue_makes_calendar_style_verdict(self):
        # No questions asked here: strip the fixture question run by
        # pointing need_you at issues only via an unquestioned run.
        from openmainty import store
        for run in store.list_runs(200):
            if run.get("id") == "run-testm1q1":
                run["waitReason"] = None
                store.save(run)
        self._queue([self._item(
            "run-6a243a45", "Migrate to material_ui/cupertino_ui (4.0.0)")])
        snap = batches.refresh_summary_snapshot("test")
        row = snap["repos"][0]
        self.assertEqual(row["verdict"], "attention")
        self.assertEqual(row["verdict_rule"], "waiting-review")


class FoundDiffTests(unittest.TestCase):
    """D2b: name only what is new versus the previous scan."""

    def test_added_entries_only(self):
        prev = frozenset(["a->1", "b->2"])
        self.assertEqual(
            batches._m1_scan_found("a->1; b->2; c->3", prev_dset=prev),
            "새 업데이트 감지: c 3")

    def test_counts_only_change(self):
        self.assertEqual(
            batches._m1_scan_found("a->1; b->2",
                                   prev_dset=frozenset(["a->1", "b->2"]),
                                   counts=(68, 44, 14),
                                   prev_counts=(68, 43, 14)),
            "점검에서 변화 발견: 드리프트 43→44")

    def test_queued_only(self):
        self.assertEqual(batches._m1_scan_found("", queued=2), "대기 2건")


class OrphanCacheTests(RefreshFixture):
    """D10: the dropped cache's evals.json is removed on refresh."""

    def test_evals_json_removed(self):
        from openmainty import paths as _paths
        target = _paths.home() / "summary" / "evals.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}", encoding="utf-8")
        batches.refresh_summary_snapshot("test")
        self.assertFalse(target.exists())


class KstStampTests(unittest.TestCase):
    """P1-4: pre-rendered story stamps read in KST, never raw ISO."""

    def test_kst_short(self):
        self.assertEqual(batches._m1_kst_short(
            "2026-10-17T18:42:00+00:00"), "10/18 03:42")

    def test_unparseable_passes_through(self):
        self.assertEqual(batches._m1_kst_short("not-a-date"),
                         "not-a-date")
        self.assertEqual(batches._m1_kst_short(""), "")


class StoryWindowTests(RefreshFixture):
    """P1-3: the 24h story drops 3-day-old events and scans alike."""

    def _backdate_last_event(self, days):
        import datetime
        import json
        f = self.events._file()
        lines = f.read_text(encoding="utf-8").splitlines()
        ev = json.loads(lines[-1])
        old = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.timedelta(days=days)).isoformat()
        ev["at"] = old
        lines[-1] = json.dumps(ev, ensure_ascii=False)
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return old

    def test_old_run_done_dropped(self):
        self._backdate_last_event(3)
        story = batches._m1_story("fakerepo", [], cap=False)
        self.assertFalse(any("끝냄" in t for t in story))

    def test_old_scans_dropped(self):
        import datetime
        import json
        old = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.timedelta(days=3)).isoformat()
        f = self.events._file()
        lines = []
        for ln in f.read_text(encoding="utf-8").splitlines():
            ev = json.loads(ln)
            ev["at"] = old
            lines.append(json.dumps(ev, ensure_ascii=False))
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")
        story = batches._m1_story("fakerepo", [], cap=False)
        self.assertEqual(story, [])


class RunDoneWordingTests(unittest.TestCase):
    """P2-8: dep-bump titles read as 'dep old → new 검증 끝냄'."""

    def test_bump_shape(self):
        self.assertEqual(
            batches.m1_event_sentence(
                "run-done",
                "chore(deps): cupertino_icons 1.0.9 -> 2.0.0",
                "proof"),
            "cupertino_icons 1.0.9 → 2.0.0 검증 끝냄 (근거 있음)")

    def test_non_bump_kept(self):
        self.assertEqual(
            batches.m1_event_sentence("run-done", "foo 1.0", ""),
            "Muse가 foo 1.0 끝냄")


class QuestionPrefixTests(RefreshFixture):
    """P2-9: questions carry '#issue title —' so the owner sees context."""

    def test_issue_prefix(self):
        from openmainty import store
        for run in store.list_runs(200):
            if run.get("id") == "run-testm1q1":
                run["issue"] = "279"
                run["title"] = "릴리스 빌드에서 소리 안 남"
                store.save(run)
        snap = batches.refresh_summary_snapshot("test")
        q = snap["repos"][0]["need_you"][0]["question"]
        self.assertTrue(
            q.startswith("#279 릴리스 빌드에서 소리 안 남 — "), q)

    def test_no_issue_no_prefix(self):
        snap = batches.refresh_summary_snapshot("test")
        q = snap["repos"][0]["need_you"][0]["question"]
        self.assertTrue(q.startswith("어느 버전으로"), q)


class BranchDaysTests(RefreshFixture):
    """P2-10: branch tips carry day counts for '(N일 전)'."""

    def test_main_days_present(self):
        rel = batches.m1_release_info(str(self.checkout))
        self.assertIsInstance(rel["main_days"], int)
        self.assertGreaterEqual(rel["main_days"], 0)
        self.assertIsNone(rel["next_days"])
        snap = batches.refresh_summary_snapshot("test")
        got = snap["repos"][0]["pending_release"]
        self.assertEqual(got["main_days"], rel["main_days"])


class NextStepsTests(unittest.TestCase):
    """P1-6: the next-step line is derived from data, never invented."""

    def _batch(self, bid, lane, ready=True, inherited=None):
        results = [{"ready": True}] if ready else [{"ready": False}]
        return {"id": bid, "target_branch": lane,
                "primaries": [{"dep": "d-" + bid, "target": "1.0"}],
                "inherited": [{"batch": h} for h in (inherited or [])],
                "_results": results}

    def _pre(self, buds):
        return {b["id"]: {"results": b.pop("_results")} for b in buds}

    def test_two_leaves_point_at_rollup(self):
        buds = [self._batch("base", "next"),
                self._batch("l1", "next", inherited=["base"]),
                self._batch("l2", "next", inherited=["base"])]
        pre = self._pre(buds)
        steps = batches._m1_next_steps("r", buds, pre, {})
        self.assertEqual(len(steps), 1)
        self.assertIn("next 레인 2개 합치기 가능", steps[0]["text"])
        self.assertNotIn("나머지", steps[0]["text"])
        self.assertIn("tab=reviews", steps[0]["link"])

    def test_panel_count_with_other_base_rest(self):
        # Tonight's shape: 2 leaves share base, 1 leaf lives on
        # another base. The line matches the panel (2), naming the
        # rest honestly instead of claiming all 3.
        buds = [self._batch("base", "next"),
                self._batch("other", "next"),
                self._batch("l1", "next", inherited=["base"]),
                self._batch("l2", "next", inherited=["base"]),
                self._batch("l3", "next", inherited=["other"])]
        pre = self._pre(buds)
        steps = batches._m1_next_steps("r", buds, pre, {})
        self.assertEqual(len(steps), 1)
        self.assertIn("2개 합치기 가능", steps[0]["text"])
        self.assertIn("나머지 1개는 다른 기준", steps[0]["text"])

    def test_multi_primary_base_still_anchors(self):
        # Tonight: the panel base carries 2 primaries, so it is never
        # a leaf, but it still anchors the panel line for its leaves.
        base = {"id": "pairbase", "target_branch": "next",
                "primaries": [{"dep": "a", "target": "1.0"},
                              {"dep": "b", "target": "2.0"}],
                "inherited": [],
                "_results": [{"ready": True}]}
        buds = [base,
                self._batch("l1", "next", inherited=["pairbase"]),
                self._batch("l2", "next", inherited=["pairbase"])]
        pre = self._pre(buds)
        pre["pairbase"] = {"results": [{"ready": True},
                                       {"ready": True}]}
        steps = batches._m1_next_steps("r", buds, pre, {})
        self.assertEqual(len(steps), 1)
        self.assertIn("2개 합치기 가능", steps[0]["text"])
        self.assertNotIn("나머지", steps[0]["text"])

    def test_hold_line_replaces_waiting_with_deep_link(self):
        # M2.5b: a held auto batch shows its hold (who + why) with
        # a card link, and leaves the waiting count.
        held = {"id": "auto-h", "target_branch": "next",
                "origin_m2": "prep-h",
                "primaries": [{"dep": "d", "target": "1.0"}],
                "inherited": [],
                "hold": {"by": "root", "reason": "metro lockstep",
                         "at": "t"},
                "policy": {"required_gates": ["unit"],
                           "gates": {"unit": {"root": True}}},
                "integrations": [{"lane": "next", "gates": []}]}
        wait = {"id": "auto-w", "target_branch": "next",
                "origin_m2": "prep-w",
                "primaries": [{"dep": "e", "target": "2.0"}],
                "inherited": [],
                "policy": {"required_gates": ["unit"],
                           "gates": {"unit": {"root": True}}},
                "integrations": [{"lane": "next", "gates": []}]}
        steps = batches._m1_next_steps("r", [held, wait], {}, {})
        kinds = [s["kind"] for s in steps]
        self.assertIn("batch-hold", kinds)
        self.assertIn("root-review", kinds)
        hold = [s for s in steps if s["kind"] == "batch-hold"][0]
        self.assertIn("보류: metro lockstep — root", hold["text"])
        self.assertIn("batch=auto-h", hold["link"])
        wait_step = [s for s in steps if s["kind"] == "root-review"][0]
        self.assertIn("Root 확인 대기 묶음 1개 (게이트 1건)",
                      wait_step["text"])

    def test_single_waiting_batch_deep_links(self):
        wait = {"id": "auto-w", "target_branch": "next",
                "origin_m2": "prep-w",
                "primaries": [{"dep": "e", "target": "2.0"}],
                "inherited": [],
                "policy": {"required_gates": ["unit"],
                           "gates": {"unit": {"root": True}}},
                "integrations": [{"lane": "next", "gates": []}]}
        steps = batches._m1_next_steps("r", [wait], {}, {})
        self.assertEqual(len(steps), 1)
        self.assertIn("batch=auto-w", steps[0]["link"])

    def test_other_lane_base_ignored(self):
        buds = [self._batch("base", "next"),
                self._batch("mainbase", "main"),
                self._batch("l1", "next", inherited=["base"]),
                self._batch("l2", "next", inherited=["base"]),
                self._batch("m1", "next", inherited=["mainbase"])]
        pre = self._pre(buds)
        steps = batches._m1_next_steps("r", buds, pre, {})
        self.assertEqual(len(steps), 1)
        self.assertIn("2개 합치기 가능", steps[0]["text"])
        self.assertIn("나머지 1개는 다른 기준", steps[0]["text"])

    def test_single_leaf_no_line(self):
        buds = [self._batch("base", "next"),
                self._batch("l1", "next", inherited=["base"])]
        pre = self._pre(buds)
        self.assertEqual(batches._m1_next_steps("r", buds, pre, {}), [])

    def test_pr_draft_wins(self):
        buds = [self._batch("base", "next"),
                self._batch("l1", "next", inherited=["base"]),
                self._batch("l2", "next", inherited=["base"])]
        pre = self._pre(buds)
        rollups = {"next": {"status": "done", "pr_title": "chore(deps): all"}}
        steps = batches._m1_next_steps("r", buds, pre, rollups)
        self.assertEqual(len(steps), 1)
        self.assertIn("PR 초안 확인", steps[0]["text"])

    def test_blocked_points_at_reason(self):
        rollups = {"main": {"status": "blocked", "error": "충돌"}}
        steps = batches._m1_next_steps("r", [], {}, rollups)
        self.assertEqual(len(steps), 1)
        self.assertIn("막힘 사유", steps[0]["text"])


class ReleaseTagTests(RefreshFixture):
    """D5: tags labeled as releases; branch tips labeled as 반영."""

    def test_tag_basis(self):
        git("tag", "v1.0.0", cwd=self.checkout)
        snap = batches.refresh_summary_snapshot("test")
        rel = snap["repos"][0]["pending_release"]
        self.assertEqual(rel["release_tag"], "v1.0.0")
        self.assertEqual(rel["basis"], "tag")
        self.assertEqual(rel["main_date"], "2026-09-06")

    def test_branch_basis_without_tags(self):
        snap = batches.refresh_summary_snapshot("test")
        rel = snap["repos"][0]["pending_release"]
        self.assertEqual(rel["release_tag"], "")
        self.assertEqual(rel["basis"], "branches")
        self.assertEqual(rel["main_date"], "2026-09-06")


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_temp_home(self, str(Path(self.tmp.name) / "home"))
        from openmainty import serve  # noqa: E402
        self.serve = serve
        self._gate = serve._require_gate
        serve._require_gate = lambda: None
        self.addCleanup(setattr, serve, "_require_gate", self._gate)

    def test_summary_unknown_before_refresh(self):
        out = self.serve.api_summary({})
        self.assertTrue(out.get("unknown"))
        self.assertEqual(out.get("repos"), [])
        self.assertTrue(out.get("served_at"))

    def test_summary_serves_snapshot_fast(self):
        batches.refresh_summary_snapshot("test")
        start = time.time()
        out = self.serve.api_summary({})
        elapsed_ms = (time.time() - start) * 1000
        self.assertFalse(out.get("unknown"))
        self.assertTrue(out.get("served_at"))
        self.assertLess(elapsed_ms, 200)
        raw = batches.summary_snapshot_path().read_bytes()
        self.assertLessEqual(len(raw), 30 * 1024)

    def test_digest_endpoints(self):
        with self.assertRaises(self.serve.ApiError) as ctx:
            self.serve.api_digest({"date": "../evil"})
        self.assertEqual(ctx.exception.status, 400)
        missing = self.serve.api_digest({"date": "1999-01-01"})
        self.assertTrue(missing.get("unknown"))
        batches.write_digest("2026-10-20")
        got = self.serve.api_digest({"date": "2026-10-20"})
        self.assertEqual(got["date"], "2026-10-20")


HTML = Path(__file__).resolve().parent.parent / "src" / "openmainty" \
    / "dashboard.html"


class GlanceStructureTests(unittest.TestCase):
    """Inline render sites: structural asserts only. Visible behavior is
    verified in the real browser during self-use."""

    def test_routes_and_labels(self):
        src = HTML.read_text(encoding="utf-8")
        for token in (
                'seg[0] === "glance"',
                'seg[0] === "digest"',
                "renderGlanceView",
                "renderDigestView",
                "navglance",
                "navdigest",
                "/api/summary",
                "/api/digest",
                "한눈에 보기",
                "하루 요약",
                "사람이 할 일",
                "막힌 것과 이유",
                "24시간 이야기",
                "저장소 현황 (PR·브랜치·이야기·시스템)",
                "마지막 갱신",
                "Muse 제안 — 보내기 전 확인",
                "제안 담기",
                "suggest-",
                "dataset.suggested",
                "용어 풀이",
                "다음 큰 버전을 위한 개발 갈래",
                "빠른 검사: 대기 중 (최종 확인 전)",
                "마지막 반영",
                "일 전)",
                "출시 태그",
                "next_steps",
                "검토 작업실에서 보기",
        ):
            self.assertIn(token, src, "glance token missing: %s" % token)
        # The suggestion fills the box; it never sends.
        self.assertNotIn("suggest-send", src)
        self.assertIn("applyPendingSuggestion", src)


class DigestTickTests(RefreshFixture):
    """Live defect (2026-10-21): sched._m1_after_tick referenced an
    unassigned `snap`, so every tick wrote '하루 요약 작성 실패'
    (NameError) and no provisional digest for ~7 h. The tick path —
    not write_digest directly — is what these tests exercise."""

    def test_tick_writes_provisional_digest_without_failure_events(self):
        from openmainty import sched
        sched._m1_after_tick()
        titles = [e.get("title") for e in
                  self.events.list_events("", limit=200)]
        self.assertNotIn("하루 요약 작성 실패", titles)
        self.assertNotIn("상태 요약 갱신 실패", titles)
        today = batches.m1_local_day()
        self.assertTrue(batches.digest_file(today).is_file())
        self.assertTrue(batches.digest_md_file(today).is_file())

    def test_tick_survives_snapshot_failure_with_fallback(self):
        from openmainty import sched
        real = batches.refresh_all_snapshots
        batches.refresh_all_snapshots = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("registry down"))
        try:
            sched._m1_after_tick()  # must not propagate
        finally:
            batches.refresh_all_snapshots = real
        titles = [e.get("title") for e in
                  self.events.list_events("", limit=200)]
        # The failure is recorded honestly, and the digest still
        # lands via write_digest's own fallback snapshot.
        self.assertIn("상태 요약 갱신 실패", titles)
        self.assertNotIn("하루 요약 작성 실패", titles)
        self.assertTrue(
            batches.digest_file(batches.m1_local_day()).is_file())


if __name__ == "__main__":
    unittest.main()
