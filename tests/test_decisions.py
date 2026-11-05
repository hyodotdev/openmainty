"""Decision desk projection: offline identity/freshness/unknown tests.

Synthetic fixtures only, in a fresh isolated OPENMAINTY_HOME set
before any product import. No network, no real home/target.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import decisions as d


def make_home(test):
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    prev = os.environ.get("OPENMAINTY_HOME")
    os.environ["OPENMAINTY_HOME"] = str(Path(tmp.name) / "home")
    test.addCleanup(lambda: os.environ.pop("OPENMAINTY_HOME", None)
                    if prev is None else os.environ.__setitem__(
                        "OPENMAINTY_HOME", prev))
    return Path(tmp.name)


def write_snapshot(rows, at="2026-10-27T00:00:00+00:00"):
    from openmainty import batches
    from openmainty.util import write_json
    snap = {"schema": 1, "at": at, "writer": "test",
            "repos": rows, "refresh_obs": {}}
    path = batches.summary_snapshot_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, snap)
    return snap


def repo_row(name, need_you, blocked=None):
    return {"name": name, "need_you": need_you,
            "blocked": blocked if blocked is not None else [],
            "verdict": "normal"}


def question_entry(ref, question="Q?", age=3, why="W", suggested="S"):
    return {"kind": "question", "ref": ref, "question": question,
            "age_days": age, "age_text": "%d일째 대기" % age, "why": why,
            "suggested": suggested, "suggested_basis": "B"}


def save_run(run_id, repo, issue="431", wait="question: full text here",
             fingerprint="f" * 64):
    from openmainty import store
    state = {"id": run_id, "repo": repo, "issue": issue,
             "title": "T", "pc": "pc", "worker": "w",
             "created_at": "t", "baseCommit": "b" * 40,
             "workCommit": "c" * 40, "verifiedCommits": {},
             "workTreeFingerprint": fingerprint,
             "verifiedFingerprints": {}, "sourceKind": "git",
             "steps": {"triage": {"status": "blocked"},
                       "fix": {"status": "pending"}},
             "artifacts": {}, "deviceInfo": {}, "interventions": [],
             "waitReason": wait}
    store.save(state)
    return state


def save_profile(repo="cal", slug="o/cal"):
    from openmainty import repos
    repos.save({"name": repo, "github": slug, "baseBranch": "main",
                "knowledge": [], "checks": [], "e2e": [],
                "release": {}, "announce": {}})


class DeskSnapshotTest(unittest.TestCase):
    def setUp(self):
        make_home(self)

    def test_supported_and_unsupported_split_with_denominators(self):
        write_snapshot([repo_row("cal", [
            question_entry("run-1"),
            {"kind": "prepare-retry", "ref": "run-9",
             "question": "retry", "age_days": 1,
             "age_text": "1일째", "why": "", "suggested": "",
             "suggested_basis": ""},
        ])])
        out = d.desk_snapshot()
        self.assertFalse(out["unknown"])
        self.assertEqual(len(out["supported"]), 1)
        self.assertEqual(len(out["unsupported"]), 1)
        sup = out["supported"][0]
        self.assertEqual(sup["id"], "cal:question:run-1")
        self.assertTrue(sup["title_is_excerpt"])
        self.assertEqual(out["repos"][0]["counts"],
                         {"need_you_total": 2, "need_you_supported": 1,
                          "blocked_total": 0})
        uns = out["unsupported"][0]
        self.assertIn("prepare-retry", uns.get("support_note") or "")
        # Unsupported rows are disclosures, never questions.
        self.assertNotIn("question", (uns.get("title_excerpt") or "").lower()
                         .replace("retry", ""))

    def test_wrong_shapes_are_unknown_not_healthy_empty(self):
        for bad in ({"repos": {"cal": []}},
                    {"at": "t", "repos": [{"name": "cal",
                                           "need_you": {"ref": "x"}}]},
                    {"at": "t", "repos": [{"name": "cal",
                                           "need_you": ["s"]}]},
                    {"at": "t", "repos": [{"name": "cal",
                                           "need_you": [],
                                           "blocked": "x"}]}):
            from openmainty import batches
            from openmainty.util import write_json
            path = batches.summary_snapshot_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            write_json(path, bad)
            out = d.desk_snapshot()
            self.assertTrue(out["unknown"], bad)
            self.assertTrue(out["error"])
            self.assertEqual(out["supported"], [])

    def test_missing_snapshot_is_unknown(self):
        out = d.desk_snapshot()
        self.assertTrue(out["unknown"])
        self.assertEqual(out["supported"], [])


class RevisionTest(unittest.TestCase):
    def base_inputs(self):
        return d.revision_inputs("cal", "question", "run-1",
                                 "question: abc", "f" * 64,
                                 "b" * 40, "c" * 40,
                                 {"triage": {"status": "blocked"}})

    def test_same_id_content_mutation_is_stale(self):
        base = self.base_inputs()
        changed = d.revision_inputs("cal", "question", "run-1",
                                    "question: edited text", "f" * 64,
                                    "b" * 40, "c" * 40,
                                    {"triage": {"status": "blocked"}})
        self.assertEqual(d.derive_status(changed, base), "stale")

    def test_fingerprint_change_is_stale(self):
        base = self.base_inputs()
        changed = dict(base, work_tree_fingerprint="0" * 64)
        self.assertEqual(d.derive_status(changed, base), "stale")

    def test_identical_is_current(self):
        base = self.base_inputs()
        self.assertEqual(d.derive_status(dict(base), base), "current")

    def test_observation_stamps_excluded_from_inputs(self):
        # Volatile stamps live outside inputs by construction: age,
        # served_at, snapshot revision labels can move without
        # changing the verdict. Equal content stays current.
        base = self.base_inputs()
        for volatile in ("age_days", "age_text", "served_at",
                         "fetched_at", "at", "revision", "obs"):
            self.assertNotIn(volatile, base)
        self.assertEqual(d.derive_status(dict(base), base), "current")

    def test_empty_cross_family_and_missing_are_unknown(self):
        base = self.base_inputs()
        self.assertEqual(d.derive_status({}, {}), "unknown")
        self.assertEqual(d.derive_status(base, None), "unknown")
        self.assertEqual(d.derive_status(None, base), "unknown")
        other = d.revision_inputs("cal", "question", "run-2",
                                  "question: abc", "f" * 64,
                                  "b" * 40, "c" * 40, {})
        self.assertEqual(d.derive_status(other, base), "unknown")
        old = dict(base, version=0)
        self.assertEqual(d.derive_status(old, base), "unknown")
        self.assertFalse(d.inputs_equal({}, {}))

    def test_version_only_records_are_unknown(self):
        self.assertEqual(d.derive_status({"version": 1}, {"version": 1}),
                         "unknown")
        self.assertFalse(d.inputs_equal({"version": 1}, {"version": 1}))

    def test_coordinates_only_records_are_unknown(self):
        coord = {"version": 1, "repo": "cal", "kind": "question",
                 "ref": "run-1"}
        self.assertEqual(d.derive_status(dict(coord), dict(coord)),
                         "unknown")
        self.assertFalse(d.inputs_equal(dict(coord), dict(coord)))

    def test_bool_version_never_equals_contract(self):
        base = self.base_inputs()
        bool_ver = dict(base, version=True)
        self.assertFalse(d.inputs_equal(bool_ver, base))
        self.assertFalse(d.inputs_equal(base, bool_ver))
        self.assertEqual(d.derive_status(bool_ver, base), "unknown")
        self.assertEqual(d.derive_status(base, bool_ver), "unknown")

    def test_extra_key_breaks_shape(self):
        base = self.base_inputs()
        extra = dict(base, injected="x")
        self.assertFalse(d.inputs_equal(extra, base))
        self.assertEqual(d.derive_status(extra, base), "unknown")

    def test_null_fingerprint_is_comparable_not_live_proof(self):
        # Legitimate absent source identity compares (None == None)
        # without claiming a live worktree was verified.
        left = d.revision_inputs("cal", "question", "run-1",
                                 "question: q", None, "b" * 40, "c" * 40,
                                 {})
        right = d.revision_inputs("cal", "question", "run-1",
                                  "question: q", None, "b" * 40, "c" * 40,
                                  {})
        self.assertEqual(d.derive_status(left, right), "current")

    def test_non_string_content_is_invalid_shape(self):
        base = self.base_inputs()
        bad = dict(base, work_tree_fingerprint=12345)
        self.assertFalse(d.inputs_equal(bad, base))
        self.assertEqual(d.derive_status(bad, base), "unknown")


class DetailTest(unittest.TestCase):
    def setUp(self):
        make_home(self)
        write_snapshot([repo_row("cal", [question_entry("run-1")])])
        save_run("run-1", "cal")
        save_profile()

    def test_happy_path_binds_exact_record(self):
        out = d.decision_detail("cal", "cal:question:run-1")
        self.assertFalse(out["unknown"])
        self.assertEqual(out["question_full"], "full text here")
        self.assertTrue(out["question_pending"])
        self.assertEqual(out["inputs"]["work_tree_fingerprint"], "f" * 64)
        self.assertEqual(out["inputs"]["base_commit"], "b" * 40)
        self.assertEqual(out["steps"]["triage"], "blocked")
        self.assertEqual(out["issue_url"],
                         "https://github.com/o/cal/issues/431")

    def test_inactive_when_not_a_current_member(self):
        save_run("run-9", "cal", issue="432")
        out = d.decision_detail("cal", "cal:question:run-9")
        self.assertTrue(out["unknown"])
        self.assertTrue(out.get("inactive"))
        # The run file exists but is not a current decision.

    def test_unsafe_ref_never_reads(self):
        from openmainty import store
        with mock.patch.object(store, "load",
                               side_effect=AssertionError("must not read")):
            out = d.decision_detail("cal", "cal:question:../escape")
        self.assertTrue(out["unknown"])

    def test_foreign_run_binding_refused(self):
        save_run("run-1", "other-repo")
        out = d.decision_detail("cal", "cal:question:run-1")
        self.assertTrue(out["unknown"])

    def test_missing_run_is_unknown(self):
        write_snapshot([repo_row("cal", [question_entry("run-x")])])
        out = d.decision_detail("cal", "cal:question:run-x")
        self.assertTrue(out["unknown"])
        self.assertIn("unreadable", out["error"])

    def test_non_numeric_issue_stays_unlinked(self):
        save_run("run-2", "cal", issue="discussion-5")
        write_snapshot([repo_row("cal", [question_entry("run-2")])])
        out = d.decision_detail("cal", "cal:question:run-2")
        self.assertFalse(out["unknown"])
        self.assertIsNone(out["issue_url"])

    def test_symlinked_run_record_is_refused_not_followed(self):
        import json
        from openmainty import store
        run_file = Path(store.run_file("run-1"))
        # Point the expected run path at foreign bytes via symlink.
        run_file.unlink()
        foreign = run_file.parent / "foreign.json"
        foreign.write_text(json.dumps({"id": "run-1", "repo": "cal",
                                       "waitReason": "question: LEAKED"}),
                           encoding="utf-8")
        os.symlink(foreign, run_file)
        out = d.decision_detail("cal", "cal:question:run-1")
        self.assertTrue(out["unknown"])
        self.assertNotIn("LEAKED", json.dumps(out))

    def test_answered_run_has_no_pending_question(self):
        save_run("run-3", "cal", wait="idle")
        write_snapshot([repo_row("cal", [question_entry("run-3")])])
        out = d.decision_detail("cal", "cal:question:run-3")
        self.assertFalse(out["unknown"])
        self.assertFalse(out["question_pending"])
        self.assertEqual(out["question_full"], "")


class DeskHandlerTest(unittest.TestCase):
    """Actual serve GET handlers: response contracts, typed errors,
    and node/count/unknown transitions at the API layer."""

    def setUp(self):
        make_home(self)
        save_profile()

    def test_list_handler_returns_supported_with_denominators(self):
        save_run("run-1", "cal")
        write_snapshot([repo_row("cal", [question_entry("run-1")],
                                blocked=[{"what": "b"}])])
        from openmainty import serve as _serve
        out = _serve.api_decisions({"repo": "cal"})
        self.assertFalse(out.get("unknown"))
        self.assertEqual([x["id"] for x in out["supported"]],
                         ["cal:question:run-1"])
        self.assertEqual(out["unsupported"], [])
        self.assertEqual(out["repos"][0]["counts"]["blocked_total"], 1)
        self.assertEqual(out["revision"], "2026-10-27T00:00:00+00:00")

    def test_list_handler_bad_payload_is_400(self):
        from openmainty import serve as _serve
        with self.assertRaises(_serve.ApiError):
            _serve.api_decisions("not-a-dict")
        with self.assertRaises(_serve.ApiError):
            _serve.api_decision_detail({"repo": "", "id": "cal:question:run-1"})
        with self.assertRaises(_serve.ApiError):
            _serve.api_decision_detail({"repo": "cal", "id": ""})

    def test_list_handler_corrupt_snapshot_is_unknown(self):
        from openmainty import batches
        from openmainty import serve as _serve
        path = batches.summary_snapshot_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        before = b"{not json"
        path.write_bytes(before)
        out = _serve.api_decisions({"repo": "cal"})
        self.assertTrue(out.get("unknown"))
        self.assertEqual(path.read_bytes(), before)

    def test_detail_handler_returns_canonical_nine_field_inputs(self):
        save_run("run-1", "cal")
        write_snapshot([repo_row("cal", [question_entry("run-1")])])
        from openmainty import serve as _serve
        out = _serve.api_decision_detail(
            {"repo": "cal", "id": "cal:question:run-1"})
        self.assertFalse(out.get("unknown"))
        self.assertEqual(out["question_full"], "full text here")
        self.assertEqual(out["issue_url"],
                         "https://github.com/o/cal/issues/431")
        self.assertEqual(set(out["inputs"].keys()), {
            "version", "repo", "kind", "ref", "wait_reason",
            "work_tree_fingerprint", "base_commit", "work_commit", "steps"})
        self.assertEqual(out["inputs"]["version"], 1)
        self.assertEqual(out["inputs"]["work_tree_fingerprint"], "f" * 64)

    def test_detail_handler_missing_and_bad_ids_are_unknown(self):
        write_snapshot([repo_row("cal", [question_entry("run-9")])])
        from openmainty import serve as _serve
        missing = _serve.api_decision_detail(
            {"repo": "cal", "id": "cal:question:run-9"})
        self.assertTrue(missing.get("unknown"))
        malformed = _serve.api_decision_detail(
            {"repo": "cal", "id": "cal:not a valid id"})
        self.assertTrue(malformed.get("unknown"))
        foreign = _serve.api_decision_detail(
            {"repo": "cal", "id": "nitro:question:run-1"})
        self.assertTrue(foreign.get("unknown"))

    def test_detail_node_count_unknown_transitions(self):
        from openmainty import serve as _serve
        save_run("run-1", "cal")
        write_snapshot([repo_row("cal", [question_entry("run-1")])])
        first = _serve.api_decisions({"repo": "cal"})
        self.assertEqual(len(first["supported"]), 1)
        save_run("run-2", "cal")
        write_snapshot([repo_row(
            "cal", [question_entry("run-1"), question_entry("run-2")])],
            at="2026-10-27T01:00:00+00:00")
        second = _serve.api_decisions({"repo": "cal"})
        self.assertEqual(len(second["supported"]), 2)
        det2 = _serve.api_decision_detail(
            {"repo": "cal", "id": "cal:question:run-2"})
        self.assertFalse(det2.get("unknown"))
        from openmainty import batches
        path = batches.summary_snapshot_path()
        path.write_bytes(b"{not json")
        down = _serve.api_decisions({"repo": "cal"})
        self.assertTrue(down.get("unknown"))
        self.assertEqual(down.get("supported"), [])
        write_snapshot([repo_row("cal", [question_entry("run-1")])],
                       at="2026-10-27T02:00:00+00:00")
        back = _serve.api_decisions({"repo": "cal"})
        self.assertFalse(back.get("unknown"))
        self.assertEqual(len(back["supported"]), 1)


if __name__ == "__main__":
    unittest.main()
