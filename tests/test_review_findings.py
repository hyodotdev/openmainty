"""Reviewer VALID-findings detection over real exec --json streams.

Regression for run-fed7e06a: the reviewer emitted three concrete
`- VALID:` findings in its human terminal text, but `run_review` scanned
the raw serialized JSONL event lines for line-leading `VALID:` and
concluded "reviewer found no VALID findings" — binding review green on
an artifact-only commit. These tests feed faithful exec --json streams
(JSONL envelopes + a run_terminal.completed payload, the shape the real
`review.reviewer.log.txt` has) and require the findings to be honored.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import runner, store  # noqa: E402
try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402


TERMINAL_FINDINGS = """Scope verified. I received the recorded scope via `git diff efd5e5411813f04dd8026ff88cb2c28d0012f3ee..HEAD`: exactly 1 file, `DRAFT-babel-8-incompatibility.md`, 43 insertions. Working tree is clean, so bare `git diff` empty is expected. Both manifests are still `^7.29.7` in HEAD, identical to base.

Review (fresh reviewer, no edits made):

- VALID: committed file contradicts its own header. Line 1 says "do not commit as docs", yet it is committed on `codex/next/babel-core-8-0-7`. Merging this to `next` makes it permanent root clutter. Fix: leave as untracked draft or delete before any merge.
- VALID: commit message is wrong. `fix: chore(deps): @babel/core 7.29.7 -> 8.0.7` claims a bump the diff does not contain. Verified: base and HEAD both pin `^7.29.7`. Fix the message or do not land it.
- VALID: branch does not deliver `task_kind: dep-update` target `8.0.7`. Zero manifest or lockfile delta. If the incompatibility conclusion holds, the right outcome is close without merging, not merging a no-op plus draft note into `next`.
- NIT: peer-range evidence is plausible and partly corroborated, but no resolver output was captured."""

TERMINAL_CLEAN = """Scope verified: base..HEAD shows the `@babel/core` bump in both manifests plus the refreshed lockfile entries. Checks passed.

Review (fresh reviewer, no edits made):

- NIT: consider a comment on why the caret range was kept.
- INVALID: suggested pinning exact versions — rejected, repo convention uses carets.

VALID: none. No must-fix findings; safe to proceed."""


def _envelope(payload_type, payload, seq):
    return json.dumps({
        "schema_version": 1,
        "id": f"018f0000-0000-7000-8000-{seq:012d}",
        "stream": {"kind": "session", "id": "01test-session"},
        "sequence": seq,
        "recorded_at": 1792271183543000 + seq,
        "record_type": "event",
        "durability": "durable",
        "causation_id": "01test-run",
        "payload_type": payload_type,
        "payload_schema_version": 1,
        "payload": payload,
    })


def make_stream(terminal_text):
    """A faithful exec --json stream: deltas then one terminal envelope."""
    lines = []
    words = terminal_text.split()
    for i in range(0, len(words), 40):
        frag = " ".join(words[i:i + 40])
        lines.append(_envelope("run.output.delta", {
            "kind": "run_output_delta",
            "command_id": "01test-run",
            "run_stream": {"kind": "run", "id": "01test-run"},
            "text": frag,
        }, 100 + i))
    lines.append(_envelope("run.terminal.completed", {
        "kind": "run_terminal",
        "command_id": "01test-run",
        "run_stream": {"kind": "run", "id": "01test-run"},
        "terminal": "completed",
        "text": terminal_text,
        "reason": None,
    }, 900))
    return "\n".join(lines) + "\n"


class ReviewerFindingsStreamTests(unittest.TestCase):
    def test_human_text_recovers_terminal_findings(self):
        out = runner.human_text(make_stream(TERMINAL_FINDINGS))
        self.assertIn("- VALID: committed file contradicts its own header.", out)
        self.assertIn("- VALID: commit message is wrong.", out)
        self.assertIn("- VALID: branch does not deliver", out)

    def test_concrete_valid_findings_detected(self):
        self.assertTrue(
            runner._has_valid_findings(runner.human_text(make_stream(TERMINAL_FINDINGS))))

    def test_clean_review_has_no_valid_findings(self):
        self.assertFalse(
            runner._has_valid_findings(runner.human_text(make_stream(TERMINAL_CLEAN))))

    def test_explicit_finding_wins_over_no_findings_prose(self):
        text = ("- VALID: committed draft file must be removed before merge.\n\n"
                "The contributor guide says to write no VALID findings when the "
                "change is clean (quoted instruction, not this review's verdict).")
        self.assertTrue(runner._has_valid_findings(text))

    def test_valid_none_is_not_a_finding(self):
        self.assertFalse(runner._has_valid_findings("VALID: none"))
        self.assertFalse(runner._has_valid_findings("reviewer found no VALID findings"))

    def test_codex_probe_finding_wins_over_quoted_none(self):
        # Independent probe input: a real explicit finding on its own
        # line plus an unrelated `VALID: none` example line. The real
        # finding must win; the negative line must not mask it.
        text = ("- VALID: bug remains.\n"
                "Quoted format: VALID: none")
        self.assertTrue(runner._has_valid_findings(text))

    def test_quoted_and_header_lines_are_not_findings(self):
        self.assertFalse(runner._has_valid_findings(
            '> VALID: quoted from an earlier message, not this review'))
        self.assertFalse(runner._has_valid_findings(
            '- "VALID: none" (quoted instruction for clean changes)'))
        self.assertFalse(runner._has_valid_findings(
            "VALID findings from the previous round:\n- NIT: wording"))
        self.assertFalse(runner._has_valid_findings(
            "The guide says to write no VALID findings when clean."))

    def test_none_verdict_with_surrounding_prose_stays_negative(self):
        text = ("Scope verified: base..HEAD shows the bump.\n\n"
                "VALID: none. No must-fix findings; safe to proceed.\n"
                "The contributor guide quote above says write VALID: none.")
        self.assertFalse(runner._has_valid_findings(text))


class RunReviewHonorsFindingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_temp_home(self, str(Path(self.tmp.name) / "home"))

    def _state(self):
        work = Path(self.tmp.name) / "work"
        work.mkdir()
        state = store.create("r", "dep-x-1", "t", "", {"worker": "test"})
        return state, work

    def _profile(self):
        return {"name": "r", "github": "e/r", "baseBranch": "main", "knowledge": []}

    def test_findings_reach_apply_session(self):
        """The run-fed7e06a failure: findings hidden, review bound green."""
        state, work = self._state()
        profile = self._profile()
        stream = make_stream(TERMINAL_FINDINGS)
        ex = runner.FakeExecutor(script={
            "review": [(0, stream)],
            "apply_review": [(0, "APPLIED: removed the draft file; findings addressed.")],
        })
        ok = runner.run_review(state, profile, ex, work, "body")
        self.assertIn("apply_review", ex.calls,
                      "concrete - VALID: findings must reach the apply session")
        self.assertTrue(ok)
        note = state["steps"]["review"].get("note") or ""
        self.assertNotIn("no VALID findings", note)

    def test_empty_apply_does_not_bind_green(self):
        """An apply that exits 0 while leaving findings unaddressed is not success."""
        state, work = self._state()
        profile = self._profile()
        stream = make_stream(TERMINAL_FINDINGS)
        ex = runner.FakeExecutor(script={
            "review": [(0, stream)],
            "apply_review": [(0, "done, everything looks fine, no changes needed")],
        })
        ok = runner.run_review(state, profile, ex, work, "body")
        self.assertFalse(ok, "apply exit 0 with actionable findings remaining is not success")
        self.assertEqual(state["steps"]["review"]["status"], "failed")
        self.assertNotEqual(
            store.verification_status(state).get("review"), "fresh",
            "unaddressed findings must not bind review green")


REVIEWER_BUG = ("- VALID: bug.txt still contains the bug; "
                "correct before approval.")

APPLY_SKIPPED_UNFIXED = ("SKIPPED: bug.txt remains unchanged; "
                         "could not fix the VALID finding.")


class WritingExecutor(runner.FakeExecutor):
    """FakeExecutor that really edits the fixture workdir on apply.

    Lets the tests distinguish an honest source fix from a marker-only
    report: the fingerprint comparison sees actual bytes.
    """

    def __init__(self, script=None, writes=None):
        super().__init__(script)
        self.writes = writes or {}

    def muse_step(self, step, prompt, workdir):
        for name, content in self.writes.get(step, {}).items():
            (Path(workdir) / name).write_text(content, encoding="utf-8")
        return super().muse_step(step, prompt, workdir)


class RunReviewFreshVerifyTests(unittest.TestCase):
    """Fresh bounded re-review binds (or refuses) the final scope.

    Regression for the isolated probe: reviewer reports
    `- VALID: bug.txt still contains the bug`, apply exits 0 with
    `SKIPPED: ... remains unchanged`, the file stays buggy — the run
    must report failure, never 'VALID findings applied'.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_temp_home(self, str(Path(self.tmp.name) / "home"))

    def _state(self, bug="BUG-PRESENT\n"):
        work = Path(self.tmp.name) / "work"
        work.mkdir()
        (work / "bug.txt").write_text(bug, encoding="utf-8")
        state = store.create("r", "dep-x-1", "t", "", {"worker": "test"})
        return state, work

    def _profile(self):
        return {"name": "r", "github": "e/r", "baseBranch": "main",
                "knowledge": []}

    def _review_note(self, state):
        return state["steps"]["review"].get("note") or ""

    def test_codex_probe_skipped_unfixed_does_not_bind(self):
        state, work = self._state()
        ex = runner.FakeExecutor(script={
            "review": [(0, REVIEWER_BUG)],
            "apply_review": [(0, APPLY_SKIPPED_UNFIXED)],
            "review_verify": [(0, REVIEWER_BUG)],
        })
        ok = runner.run_review(state, self._profile(), ex, work, "body")
        self.assertFalse(ok, "unfixed SKIPPED apply must not bind green")
        self.assertEqual(state["steps"]["review"]["status"], "failed")
        self.assertIn("review_verify", ex.calls,
                      "binding needs a fresh verification pass, not markers")
        self.assertIn("remain", self._review_note(state))
        self.assertNotIn("applied", self._review_note(state).lower())
        self.assertNotEqual(store.verification_status(state).get("review"),
                           "fresh")
        self.assertEqual((work / "bug.txt").read_text(encoding="utf-8"),
                         "BUG-PRESENT\n")

    def test_unchanged_but_applied_marker_still_fails(self):
        state, work = self._state()
        ex = runner.FakeExecutor(script={
            "review": [(0, REVIEWER_BUG)],
            "apply_review": [(0, "APPLIED: fixed bug.txt")],
            "review_verify": [(0, REVIEWER_BUG)],
        })
        ok = runner.run_review(state, self._profile(), ex, work, "body")
        self.assertFalse(ok, "an APPLIED marker over unchanged bytes is not proof")
        self.assertEqual(state["steps"]["review"]["status"], "failed")

    def test_unrelated_edit_does_not_bind(self):
        state, work = self._state()
        ex = WritingExecutor(
            script={
                "review": [(0, REVIEWER_BUG)],
                "apply_review": [(0, "APPLIED: polished other.txt")],
                "review_verify": [(0, REVIEWER_BUG)],
            },
            writes={"apply_review": {"other.txt": "unrelated\n"}})
        ok = runner.run_review(state, self._profile(), ex, work, "body")
        self.assertFalse(ok, "an unrelated edit leaves the finding live")
        self.assertEqual(state["steps"]["review"]["status"], "failed")

    def test_honest_resolution_binds(self):
        state, work = self._state()
        ex = WritingExecutor(
            script={
                "review": [(0, REVIEWER_BUG)],
                "apply_review": [(0, "APPLIED: removed the bug from bug.txt")],
                "review_verify": [(0, "VALID: none. Fixed; safe to proceed.")],
            },
            writes={"apply_review": {"bug.txt": "fixed\n"}})
        ok = runner.run_review(state, self._profile(), ex, work, "body")
        self.assertTrue(ok, "a real fix plus a clean re-review binds")
        self.assertIn("review_verify", ex.calls)
        self.assertEqual(state["steps"]["review"]["status"], "done")
        self.assertIn("fresh re-review clean", self._review_note(state))
        self.assertEqual(store.verification_status(state).get("review"),
                         "fresh")

    def test_real_jsonl_streams_end_to_end(self):
        finding_stream = make_stream(
            "- VALID: bug.txt still contains the bug; correct before approval.")
        clean_stream = make_stream("VALID: none. Fixed; safe to proceed.")
        state, work = self._state()
        ex = WritingExecutor(
            script={
                "review": [(0, finding_stream)],
                "apply_review": [(0, "APPLIED: removed the bug from bug.txt")],
                "review_verify": [(0, clean_stream)],
            },
            writes={"apply_review": {"bug.txt": "fixed\n"}})
        ok = runner.run_review(state, self._profile(), ex, work, "body")
        self.assertTrue(ok)
        self.assertEqual(store.verification_status(state).get("review"),
                         "fresh")

    def test_verify_nonzero_does_not_bind(self):
        state, work = self._state()
        ex = WritingExecutor(
            script={
                "review": [(0, REVIEWER_BUG)],
                "apply_review": [(0, "APPLIED: removed the bug from bug.txt")],
                "review_verify": [(1, "reviewer crashed")],
            },
            writes={"apply_review": {"bug.txt": "fixed\n"}})
        ok = runner.run_review(state, self._profile(), ex, work, "body")
        self.assertFalse(ok, "a failed re-review must not bind")
        self.assertEqual(state["steps"]["review"]["status"], "failed")

    def test_verify_missing_verdict_does_not_bind(self):
        state, work = self._state()
        ex = WritingExecutor(
            script={
                "review": [(0, REVIEWER_BUG)],
                "apply_review": [(0, "APPLIED: removed the bug from bug.txt")],
                "review_verify": [(0, "")],
            },
            writes={"apply_review": {"bug.txt": "fixed\n"}})
        ok = runner.run_review(state, self._profile(), ex, work, "body")
        self.assertFalse(ok, "a missing re-review verdict must not bind")
        self.assertEqual(state["steps"]["review"]["status"], "failed")


UNKNOWN_VERIFY = ("I cannot inspect the final diff. "
                  "No verdict is available.")

UNKNOWN_INITIAL = ("The change looks reasonable. No obvious problems found. "
                   "LGTM.")


class RunReviewUnknownVerdictTests(unittest.TestCase):
    """Unknown reviewer text is never clean (runtime21 unknown-review).

    Regression for the root-runtime21-unknown-review receipt: reviewer
    `- VALID: bug.txt remains broken`, apply exit 0 with an APPLIED
    marker but no actual fix (source unchanged), verify
    `I cannot inspect the final diff. No verdict is available.` — yet the
    run returned True with note 'VALID findings applied; fresh re-review
    clean'. Any nonempty text that merely lacks a VALID line used to count
    as clean. Now BOTH the initial reviewer and the fresh re-reviewer
    need an explicit recognizable clean verdict, all three usage/session
    receipts land on success, and unknown/refusal text fails honestly.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_temp_home(self, str(Path(self.tmp.name) / "home"))

    def _state(self, bug="BUG-PRESENT\n"):
        work = Path(self.tmp.name) / "work"
        work.mkdir()
        (work / "bug.txt").write_text(bug, encoding="utf-8")
        state = store.create("r", "dep-x-1", "t", "", {"worker": "test"})
        return state, work

    def _profile(self):
        return {"name": "r", "github": "e/r", "baseBranch": "main",
                "knowledge": []}

    def _note(self, state):
        return state["steps"]["review"].get("note") or ""

    def test_root_probe_applied_marker_plus_unknown_verify_does_not_bind(self):
        """The exact runtime21 shape: VALID finding, APPLIED claim with no
        actual fix, inability-to-inspect verify. Must fail; source buggy."""
        state, work = self._state()
        ex = runner.FakeExecutor(script={
            "review": [(0, "- VALID: bug.txt remains broken.")],
            "apply_review": [(0, "APPLIED: claimed fix but no actual fix.")],
            "review_verify": [(0, UNKNOWN_VERIFY)],
        })
        ok = runner.run_review(state, self._profile(), ex, work, "body")
        self.assertFalse(ok, "unknown verify text must not bind green")
        self.assertEqual(state["steps"]["review"]["status"], "failed")
        self.assertNotIn("fresh re-review clean", self._note(state).lower())
        self.assertEqual((work / "bug.txt").read_text(encoding="utf-8"),
                         "BUG-PRESENT\n", "source fixture stays buggy")
        self.assertNotEqual(store.verification_status(state).get("review"),
                           "fresh")

    def test_first_review_empty_does_not_bind(self):
        state, work = self._state()
        ex = runner.FakeExecutor(script={"review": [(0, "")]})
        ok = runner.run_review(state, self._profile(), ex, work, "body")
        self.assertFalse(ok, "empty initial review is not clean")
        self.assertEqual(state["steps"]["review"]["status"], "failed")
        self.assertNotIn("apply_review", ex.calls,
                         "no findings means no apply session")
        self.assertNotEqual(store.verification_status(state).get("review"),
                           "fresh")

    def test_first_review_unknown_prose_does_not_bind(self):
        state, work = self._state()
        ex = runner.FakeExecutor(script={"review": [(0, UNKNOWN_INITIAL)]})
        ok = runner.run_review(state, self._profile(), ex, work, "body")
        self.assertFalse(ok, "unknown prose without an explicit verdict "
                             "is not clean")
        self.assertEqual(state["steps"]["review"]["status"], "failed")
        self.assertNotIn("apply_review", ex.calls)
        self.assertNotEqual(store.verification_status(state).get("review"),
                           "fresh")

    def test_first_review_refusal_does_not_bind(self):
        state, work = self._state()
        ex = runner.FakeExecutor(script={"review": [(0, UNKNOWN_VERIFY)]})
        ok = runner.run_review(state, self._profile(), ex, work, "body")
        self.assertFalse(ok, "refusal/inability-to-inspect is not clean")
        self.assertEqual(state["steps"]["review"]["status"], "failed")
        self.assertNotIn("apply_review", ex.calls)

    def test_concrete_finding_wins_over_quoted_clean_on_verify(self):
        """A real finding plus an unrelated/quoted VALID:none stays failed."""
        state, work = self._state()
        ex = WritingExecutor(
            script={
                "review": [(0, REVIEWER_BUG)],
                "apply_review": [(0, "APPLIED: removed the bug from bug.txt")],
                "review_verify": [(0, "- VALID: bug.txt remains broken.\n"
                                        "Quoted format: VALID: none")],
            },
            writes={"apply_review": {"bug.txt": "fixed\n"}})
        ok = runner.run_review(state, self._profile(), ex, work, "body")
        self.assertFalse(ok, "concrete finding beats quoted VALID:none")
        self.assertEqual(state["steps"]["review"]["status"], "failed")

    def test_real_edit_plus_marker_clean_verify_binds(self):
        """A real source fix plus an explicit marker verdict on a faithful
        JSONL stream binds green."""
        clean_stream = make_stream("VALID: none. Fixed; safe to proceed.\n"
                                   "REVIEW-VERDICT: CLEAN")
        state, work = self._state()
        ex = WritingExecutor(
            script={
                "review": [(0, REVIEWER_BUG)],
                "apply_review": [(0, "APPLIED: removed the bug from bug.txt")],
                "review_verify": [(0, clean_stream)],
            },
            writes={"apply_review": {"bug.txt": "fixed\n"}})
        ok = runner.run_review(state, self._profile(), ex, work, "body")
        self.assertTrue(ok, "real fix plus explicit clean re-review binds")
        self.assertEqual(state["steps"]["review"]["status"], "done")
        self.assertIn("fresh re-review clean", self._note(state))
        self.assertEqual(store.verification_status(state).get("review"),
                         "fresh")

    def test_success_records_all_three_usage_receipts(self):
        """The runtime21 receipt lost the verify usage on success
        (stored keys only reviewer/apply). All three must be recorded."""
        clean_stream = make_stream("VALID: none. Fixed; safe to proceed.\n"
                                   "REVIEW-VERDICT: CLEAN")
        state, work = self._state()
        ex = WritingExecutor(
            script={
                "review": [(0, REVIEWER_BUG)],
                "apply_review": [(0, "APPLIED: removed the bug from bug.txt")],
                "review_verify": [(0, clean_stream)],
            },
            writes={"apply_review": {"bug.txt": "fixed\n"}})
        self.assertTrue(runner.run_review(state, self._profile(), ex, work,
                                          "body"))
        step = state["steps"]["review"]
        self.assertEqual(set((step.get("usage") or {}).keys()),
                         {"reviewer", "apply", "verify"},
                         "reviewer/apply/verify receipts all recorded")
        self.assertIn("review.verify.log.txt", step.get("evidence") or [])
        command = " ".join(step.get("command") or [])
        self.assertIn("verify", command, "command names all three sessions")

    def test_skipped_without_fix_then_unknown_verify_does_not_bind(self):
        state, work = self._state()
        ex = runner.FakeExecutor(script={
            "review": [(0, REVIEWER_BUG)],
            "apply_review": [(0, APPLY_SKIPPED_UNFIXED)],
            "review_verify": [(0, "re-check done, nothing further to add")],
        })
        ok = runner.run_review(state, self._profile(), ex, work, "body")
        self.assertFalse(ok, "SKIPPED-no-fix plus unknown verify never binds")
        self.assertEqual(state["steps"]["review"]["status"], "failed")
        self.assertEqual((work / "bug.txt").read_text(encoding="utf-8"),
                         "BUG-PRESENT\n")


if __name__ == "__main__":
    unittest.main()
