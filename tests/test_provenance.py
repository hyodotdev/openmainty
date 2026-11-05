"""Provenance + honest-draft coverage: real vs synthetic issue identity.

Uses FakeExecutor + an isolated OPENMAINTY_HOME so no subscription quota is
spent and no target repo is touched. Run with:
  python3 -m unittest tests.test_provenance -v
"""
import argparse
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import artifacts, cli, pcconfig, planner, repos, runner, store  # noqa: E402
try:
    from tests._home import use_temp_home, offline_muse_version  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home, offline_muse_version  # noqa: E402

PROFILE = {
    "name": "fakerepo",
    "github": "example/fakerepo",
    "baseBranch": "main",
    "knowledge": [],
    "checks": [{"name": "unit", "cmd": ["make", "unit"], "requires": []}],
    "e2e": [],
    "release": {"notes": "CHANGELOG.md"},
    "announce": {"medium": True, "discord": False, "slack": False},
    "labels": {"pr": ["openmainty", "deps"]},
}

TITLE = "chore(deps): fakepkg 1.0.0 -> 2.0.0"
SYNTH = "dep-fakepkg-2.0.0"

RUN_CTX_NEXT = {
    "lane": "next",
    "branch": "codex/next/fakepkg-2-0-0",
    "base_ref": "next",
    "base_commit": "efd5e5411813f04dd8026ff88cb2c28d0012f3ee",
    "task_kind": "dep-update",
    "target": "2.0.0",
    "scopes": "dependencies",
    "knowledge": "(none)",
}


def _dep_state(**over):
    state = {
        "id": "run-fake1",
        "repo": "fakerepo",
        "issue": SYNTH,
        "title": TITLE,
        "baseCommit": "base0",
        "workCommit": "work0",
        "verifiedCommits": {},
        "dep": "fakepkg",
        "target": "2.0.0",
        "steps": {
            "reproduce": {"status": "done"},
            "review": {"status": "done"},
            "e2e": {"status": "done"},
        },
    }
    state.update(over)
    return state


class IssueIdentityTests(unittest.TestCase):
    def test_only_positive_ascii_numeric_is_real(self):
        self.assertTrue(artifacts.is_github_issue_number("431"))
        self.assertTrue(artifacts.is_github_issue_number("1"))
        for bad in ("dep-fakepkg-2.0.0", "", "0", "007", "-3", "12a",
                    " 12", "12 ", "#431", "４３１", None, 431, 4.31):
            self.assertFalse(artifacts.is_github_issue_number(bad), repr(bad))


class PrDraftTests(unittest.TestCase):
    def test_real_numeric_issue_keeps_closing_keyword(self):
        state = _dep_state(issue="431", dep=None, target=None)
        body = artifacts.pr_description(state, PROFILE, "s")
        self.assertIn("Fixes example/fakerepo#431.", body)

    def test_synthetic_missing_zero_malformed_have_no_closing(self):
        for issue in (SYNTH, "", "0", "007", "-5", "12a"):
            state = _dep_state(issue=issue)
            body = artifacts.pr_description(state, PROFILE, "s")
            self.assertNotIn("Fixes ", body, issue)
            if issue:
                self.assertNotIn("#" + issue, body, issue)
            self.assertNotIn("github.com", body, issue)
            # meaningful task/title/local run context instead
            self.assertIn(TITLE, body, issue)
            self.assertIn("run-fake1", body, issue)
            self.assertTrue(body.startswith("# " + TITLE), issue)

    def test_related_drafts_never_cite_synthetic_as_upstream(self):
        state = _dep_state()
        for text in (artifacts.release_notes(state, PROFILE, "s"),
                     artifacts.medium_draft(state, "s"),
                     artifacts.discord_draft(state),
                     artifacts.slack_draft(state)):
            self.assertNotIn(SYNTH, text)
            self.assertNotIn("http", text)
        self.assertIn(TITLE, artifacts.medium_draft(state, "s"))

    def test_related_drafts_real_issue_flow_unchanged(self):
        state = _dep_state(issue="431", dep=None, target=None,
                           title="real bug")
        self.assertIn("fixing fakerepo#431",
                      artifacts.medium_draft(state, "s"))
        self.assertIn("Fix for fakerepo#431",
                      artifacts.discord_draft(state))
        self.assertIn("[openmainty] fakerepo#431",
                      artifacts.slack_draft(state))

    def test_reproduce_skip_labeled_honestly(self):
        state = _dep_state()
        state["steps"]["reproduce"] = {
            "status": "done",
            "command": [],
            "note": ("dependency maintenance: no upstream bug to "
                     "reproduce; target fakepkg->2.0.0"),
        }
        body = artifacts.pr_description(state, PROFILE, "s")
        self.assertNotIn("reproduce@", body)
        self.assertIn("reproduce: skipped (not applicable", body)


class PlanProvenanceTests(unittest.TestCase):
    def test_next_actual_beats_main_configured(self):
        plan = planner.plan_for(PROFILE, SYNTH, TITLE, "b",
                                run_ctx=dict(RUN_CTX_NEXT))
        self.assertIn("## Actual run lane (authoritative", plan)
        self.assertIn("- lane: next", plan)
        self.assertIn("- base ref: next", plan)
        self.assertIn("efd5e5411813", plan)
        self.assertNotIn("Base branch: main", plan)
        self.assertIn("profile default only", plan)
        # header leads with the meaningful title, not repo#dep synthetic
        first = plan.splitlines()[0]
        self.assertTrue(first.startswith("# Plan: " + TITLE), first)
        self.assertIn("Local run key: fakerepo#" + SYNTH, plan)

    def test_preview_invents_no_actual_base(self):
        plan = planner.plan_for(PROFILE, SYNTH, TITLE, "b")
        self.assertIn("not recorded", plan)
        self.assertNotIn("- lane:", plan)
        # old callers keep the legacy preview shape
        self.assertTrue(plan.startswith(f"# Plan: fakerepo#{SYNTH} {TITLE}"))

    def test_real_issue_header_unchanged_with_ctx(self):
        plan = planner.plan_for(PROFILE, "431", "real bug", "b",
                                run_ctx=dict(RUN_CTX_NEXT))
        self.assertTrue(plan.startswith("# Plan: fakerepo#431 real bug"))
        self.assertIn("- lane: next", plan)


class CliWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_temp_home(self, str(Path(self.tmp.name) / "home"))
        self.checkout = Path(self.tmp.name) / "checkout"
        self.checkout.mkdir()
        (self.checkout / "README.md").write_text("# fake\n")
        repos.save(dict(PROFILE))
        pc = pcconfig.load()
        pc["checkouts"]["fakerepo"] = str(self.checkout)
        pcconfig.save(pc)

    def test_creation_wires_actual_run_context_offline(self):
        # Run-context wiring is the assertion; version string is fixture input.
        offline_muse_version(self, "fixture-muse-0.0.0")
        args = argparse.Namespace(
            repo="fakerepo", issue=SYNTH, title=TITLE,
            body="fake dep bump", body_file=None,
            allow_duplicate=False, only=None, model=None)
        fields = {"lane": "next", "branch": "codex/next/fakepkg-2-0-0",
                  "base_branch": "next", "queue_key": "k",
                  "dep": "fakepkg", "target": "2.0.0",
                  "scopes": ["dependencies"], "update_kind": "minor"}
        rc, run_id = cli._start_run_with_held(
            args, runner.FakeExecutor(), run_fields=fields)
        self.assertEqual(rc, 0)
        rdir = store.run_dir(run_id)
        plan = (rdir / "plan.md").read_text(encoding="utf-8")
        self.assertIn("- lane: next", plan)
        self.assertIn("codex/next/fakepkg-2-0-0", plan)
        self.assertIn("- base ref: next", plan)
        self.assertTrue(plan.splitlines()[0].startswith("# Plan: " + TITLE))
        pr = (rdir / "pr.md").read_text(encoding="utf-8")
        self.assertNotIn("Fixes ", pr)
        self.assertIn("reproduce: skipped (not applicable", pr)
        self.assertIn("review@", pr)


if __name__ == "__main__":
    unittest.main()
