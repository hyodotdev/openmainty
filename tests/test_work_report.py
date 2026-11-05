"""Work report grouping: registered evidence only, no model calls.

Covers serve._work_report (fix terminal text + final review text),
_review_terminal_text (last terminal wins, raw stream never surfaced),
and _rewrite_report_links (worktree links open the in-app viewer;
anything else degrades to plain label text).
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import serve, store  # noqa: E402
try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402


class ReportMixin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_temp_home(self, str(Path(self.tmp.name) / "home"))
        state = store.create("fakerepo", "dep-foo-1.2.3", "t", "b" * 40, {})
        self.run_id = state["id"]
        self.work = store.run_dir(self.run_id) / "work"
        self.work.mkdir(parents=True, exist_ok=True)

    def save_steps(self, fix_text):
        state = store.load(self.run_id)
        state["steps"] = {"fix": {"status": "done",
                                  "usage": {"terminal_text": fix_text}}}
        store.save(state)
        return state

    def write_reviewer_log(self, lines):
        (store.run_dir(self.run_id) / "review.reviewer.log.txt").write_text(
            "\n".join(lines) + "\n", encoding="utf-8")


class ReportTests(ReportMixin):
    def test_fix_and_review_grouped_with_viewer_links(self):
        fix = ("FILES CHANGED\n- [package.json]"
               f"({self.work}/package.json): bump\n"
               "- [/etc/passwd](/etc/passwd): must not link\n"
               "- [esc](/nope/../x): traversal must not link")
        state = self.save_steps(fix)
        self.write_reviewer_log([
            '{"payload": {"kind": "run_output_delta", "text": "noise"}}',
            '{"payload": {"kind": "run_terminal", "text": "old final"}}',
            "not json at all",
            '{"payload": {"kind": "run_terminal", "text": '
            '"Scope next@efd..960\\nREVIEW-VERDICT CLEAN"}}',
        ])
        rep = serve._work_report(state, self.run_id)
        self.assertFalse(rep["fix_missing"])
        self.assertFalse(rep["review_missing"])
        self.assertIn("[package.json](workfile:package.json)", rep["fix"])
        self.assertNotIn("/etc/passwd](", rep["fix"])
        self.assertNotIn("(/nope", rep["fix"])
        self.assertNotIn("/etc/passwd](", rep["fix"])
        self.assertEqual(rep["review"],
                         "Scope next@efd..960\nREVIEW-VERDICT CLEAN")
        self.assertNotIn("payload", rep["review"])

    def test_missing_evidence_is_explicit(self):
        state = self.save_steps("")
        rep = serve._work_report(state, self.run_id)
        self.assertTrue(rep["fix_missing"])
        self.assertTrue(rep["review_missing"])
        self.assertEqual(rep["fix"], "")
        self.assertEqual(rep["review"], "")

    def test_oversize_log_refuses_terminal_claim(self):
        big = store.run_dir(self.run_id) / "review.reviewer.log.txt"
        big.write_bytes(b"x" * (8 * 1024 * 1024 + 1))
        state = self.save_steps("something")
        rep = serve._work_report(state, self.run_id)
        self.assertTrue(rep["review_missing"])


class ScopeReportTests(ReportMixin):
    def set_state(self, analyze="", fix="", update_kind="",
                  head="h" * 40, fp="f" * 32):
        state = store.load(self.run_id)
        state["workCommit"] = head
        state["workTreeFingerprint"] = fp
        state["update_kind"] = update_kind
        state["steps"] = {
            "analyze": {"status": "done",
                        "usage": {"terminal_text": analyze}},
            "fix": {"status": "done",
                    "usage": {"terminal_text": fix}},
            "e2e": {"note": "3 check(s) passed"},
        }
        store.save(state)
        return state

    def write_scope(self, obj=None, raw=None, size=None):
        p = store.run_dir(self.run_id) / "review.scope.json"
        if size is not None:
            p.write_bytes(b"x" * size)
            return
        p.write_text(raw if raw is not None else json.dumps(obj),
                     encoding="utf-8")

    def test_analysis_surfaced_not_missing(self):
        state = self.set_state(analyze="DECISION: proceed\nreasons here",
                               fix="FILES CHANGED\n- a")
        rep = serve._work_report(state, self.run_id)
        self.assertFalse(rep["analysis_missing"])
        self.assertIn("DECISION: proceed", rep["analysis"])

    def test_analysis_absent_stays_missing(self):
        state = self.set_state(analyze="", fix="x")
        rep = serve._work_report(state, self.run_id)
        self.assertTrue(rep["analysis_missing"])
        self.assertEqual(rep["analysis"], "")

    def test_scope_recorded_when_matching(self):
        head, fp, base = "a" * 40, "b" * 64, "b" * 40
        state = self.set_state(fix="x", head=head, fp=fp)
        self.write_scope({"base_commit": base, "base_ref": "next",
                          "head": head, "fingerprint": fp, "lane": "next",
                          "branch": "codex/x", "base_stat": "f | 2 +-",
                          "diff_capped": False,
                          "bare_git_diff_empty": True})
        (store.run_dir(self.run_id) / "review.diff.txt").write_text(
            "diff", encoding="utf-8")
        rep = serve._work_report(state, self.run_id)
        self.assertEqual(rep["review_scope"]["status"], "recorded")
        self.assertEqual(rep["review_scope"]["head_short"], head[:12])
        self.assertEqual(rep["review_scope"]["fingerprint_short"], fp[:12])
        self.assertIn("review.scope.json", rep["review_scope_files"])
        self.assertIn("review.diff.txt", rep["review_scope_files"])

    def test_scope_missing_without_file(self):
        state = self.set_state(fix="x")
        rep = serve._work_report(state, self.run_id)
        self.assertEqual(rep["review_scope"]["status"], "missing")
        self.assertEqual(rep["review_scope_files"], [])

    def test_scope_malformed_and_mismatch_are_explicit(self):
        state = self.set_state(fix="x", head="a" * 40, fp="b" * 64)
        self.write_scope(raw="{not json")
        rep = serve._work_report(state, self.run_id)
        self.assertEqual(rep["review_scope"]["status"], "malformed")
        self.write_scope({"base_commit": "b" * 40, "head": "d" * 40,
                          "fingerprint": "e" * 64})
        rep = serve._work_report(state, self.run_id)
        self.assertEqual(rep["review_scope"]["status"], "mismatch")
        self.assertIn("불일치", rep["review_scope"]["error"])

    def test_scope_wrong_base_is_mismatch(self):
        state = self.set_state(fix="x", head="a" * 40, fp="b" * 64)
        self.write_scope({"base_commit": "d" * 40, "head": "a" * 40,
                          "fingerprint": "b" * 64})
        rep = serve._work_report(state, self.run_id)
        self.assertEqual(rep["review_scope"]["status"], "mismatch")

    def test_scope_missing_recorded_identity_is_unknown(self):
        state = self.set_state(fix="x", head="a" * 40, fp="b" * 64)
        self.write_scope({"base_commit": "b" * 40, "head": "a" * 40,
                          "fingerprint": "b" * 64})
        rep = serve._work_report(state, self.run_id)
        self.assertEqual(rep["review_scope"]["status"], "recorded")
        wiped = store.load(self.run_id)
        wiped["workTreeFingerprint"] = None
        store.save(wiped)
        rep = serve._work_report(wiped, self.run_id)
        self.assertEqual(rep["review_scope"]["status"], "unknown")
        self.assertIn("실행 기록", rep["review_scope"]["error"])

    def test_scope_partial_identity_never_recorded(self):
        state = self.set_state(fix="x", head="a" * 40, fp="b" * 64)
        self.write_scope({"base_commit": "b" * 40, "head": "a" * 40})
        rep = serve._work_report(state, self.run_id)
        self.assertEqual(rep["review_scope"]["status"], "unknown")
        self.write_scope({"base_commit": "b" * 40,
                          "fingerprint": "b" * 64})
        rep = serve._work_report(state, self.run_id)
        self.assertEqual(rep["review_scope"]["status"], "unknown")
        self.write_scope({"head": "a" * 40, "fingerprint": "b" * 64})
        rep = serve._work_report(state, self.run_id)
        self.assertEqual(rep["review_scope"]["status"], "unknown")
        self.write_scope({"base_commit": "b" * 40, "head": "a" * 40,
                          "fingerprint": "b" * 32})
        rep = serve._work_report(state, self.run_id)
        self.assertEqual(rep["review_scope"]["status"], "unknown")
        self.assertIn("불완전", rep["review_scope"]["error"])
        self.write_scope({"base_commit": "b" * 40, "head": "abc",
                          "fingerprint": "b" * 64})
        rep = serve._work_report(state, self.run_id)
        self.assertEqual(rep["review_scope"]["status"], "unknown")
        self.assertIn("불완전", rep["review_scope"]["error"])

    def test_scope_wrong_field_types_are_malformed(self):
        state = self.set_state(fix="x", head="a" * 40, fp="b" * 64)
        self.write_scope({"base_commit": "b" * 40, "head": ["a" * 40],
                          "fingerprint": "b" * 64})
        rep = serve._work_report(state, self.run_id)
        self.assertEqual(rep["review_scope"]["status"], "malformed")
        self.write_scope({"base_commit": "b" * 40, "head": "a" * 40,
                          "fingerprint": "b" * 64,
                          "base_stat": ["f | 2 +-"]})
        rep = serve._work_report(state, self.run_id)
        self.assertEqual(rep["review_scope"]["status"], "malformed")
        self.write_scope({"base_commit": "b" * 40, "head": "a" * 40,
                          "fingerprint": "b" * 64,
                          "diff_capped": "false"})
        rep = serve._work_report(state, self.run_id)
        self.assertEqual(rep["review_scope"]["status"], "malformed")

    def test_scope_outside_symlink_refused(self):
        state = self.set_state(fix="x", head="a" * 40, fp="b" * 64)
        with tempfile.TemporaryDirectory() as tmp:
            outside = os.path.join(tmp, "scope.json")
            with open(outside, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"base_commit": "b" * 40,
                                     "head": "a" * 40,
                                     "fingerprint": "b" * 64}))
            link = store.run_dir(self.run_id) / "review.scope.json"
            os.symlink(outside, link)
            rep = serve._work_report(state, self.run_id)
        self.assertEqual(rep["review_scope"]["status"], "unknown")
        self.assertIn("경로", rep["review_scope"]["error"])

    def test_scope_binding_lost_when_stored_state_moves(self):
        head, fp, base = "a" * 40, "b" * 64, "b" * 40
        state = self.set_state(fix="x", head=head, fp=fp)
        self.write_scope({"base_commit": base, "head": head,
                          "fingerprint": fp})
        rep = serve._work_report(state, self.run_id)
        self.assertEqual(rep["review_scope"]["status"], "recorded")
        drifted = store.load(self.run_id)
        drifted["workCommit"] = "f" * 40
        store.save(drifted)
        rep = serve._work_report(drifted, self.run_id)
        self.assertEqual(rep["review_scope"]["status"], "mismatch")

    def test_scope_oversized_refuses_claim(self):
        state = self.set_state(fix="x")
        self.write_scope(size=serve._REVIEW_SCOPE_READ_CAP + 1)
        rep = serve._work_report(state, self.run_id)
        self.assertEqual(rep["review_scope"]["status"], "oversized")

    def test_update_kind_and_fix_text_stay_truthful(self):
        fix = ("FILES CHANGED\n- package.json: ^1.0.0 -> ^2.0.0\n"
               "applied with manager up, not lock-only")
        state = self.set_state(fix=fix, update_kind="lock-refresh")
        rep = serve._work_report(state, self.run_id)
        self.assertEqual(rep["update_kind"], "lock-refresh")
        self.assertIn("^1.0.0 -> ^2.0.0", rep["fix"])
        self.assertIn("not lock-only", rep["fix"])

    def test_lock_only_fix_invents_no_range(self):
        state = self.set_state(fix="FILES CHANGED\n- yarn.lock: refresh",
                               update_kind="lock-refresh")
        rep = serve._work_report(state, self.run_id)
        self.assertEqual(rep["update_kind"], "lock-refresh")
        self.assertNotIn("^", rep["fix"])


if __name__ == "__main__":
    unittest.main()
