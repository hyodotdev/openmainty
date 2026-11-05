"""M2.3 driver tests: auto-rollup of READY leaves (stubbed plan).

The real-evaluate M3 tests (READY only via evaluate() on the shared
live map) need fixture batches + live checkouts and land with the
batches.py half. Here: same-set hands off, superset proceeds, busy
is a skip, refusals block, stage-off is quiet. Py3.9 portable.
"""
import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import batches, m2, repos, sched  # noqa: E402
try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402


class RollupDriverFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        use_temp_home(self, str(self.home))
        repos.save({"name": "fakerepo", "github": "o/r",
                    "baseBranch": "main"})
        m2.set_policy("fakerepo", "prepare", True)
        m2.set_policy("fakerepo", "fastcheck", True)
        m2.set_policy("fakerepo", "batch", True)
        m2.set_policy("fakerepo", "rollup", True)
        self._real_plan = m2._rollup_plan
        self._real_rollup = batches.rollup_leaves

    def tearDown(self):
        m2._rollup_plan = self._real_plan
        batches.rollup_leaves = self._real_rollup

    def done_op(self, op_id, leaves, updated_at="2026-10-21T00:00:00+00:00"):
        ops = self.home / "integration_ops"
        ops.mkdir(parents=True, exist_ok=True)
        (ops / (op_id + ".json")).write_text(json.dumps(
            {"op": "rollup", "op_id": op_id, "repo": "fakerepo",
             "lane": "next", "status": "done",
             "updated_at": updated_at,
             "leaves": [{"batch": b} for b in leaves]}),
            encoding="utf-8")


class SameSetTests(RollupDriverFixture):
    def test_same_ready_set_as_done_means_hands_off(self):
        # M2: the same READY set as a done rollup → no new op.
        self.done_op("rollup-next-base-aaa", ["leaf-1", "leaf-2"])
        m2._rollup_plan = lambda repo, lane: ("base",
                                              ["leaf-1", "leaf-2"])
        calls = []
        batches.rollup_leaves = lambda *a, **k: (calls.append((a, k)) or {})
        out = m2._run_rollup_stage("fakerepo", "next")
        self.assertFalse(out["ran"])
        self.assertEqual(out["reason"], "already rolled")
        self.assertEqual(calls, [])

    def test_strictly_larger_set_proceeds_as_new_op(self):
        # M2: a strictly larger set proceeds as a NEW op naming the
        # older done op (its drafts say "supersedes").
        self.done_op("rollup-next-base-aaa", ["leaf-1", "leaf-2"])
        m2._rollup_plan = lambda repo, lane: (
            "base", ["leaf-1", "leaf-2", "leaf-3"])
        calls = []
        batches.rollup_leaves = lambda *a, **k: (
            calls.append((a, k)) or {"op_id": "rollup-next-base-bbb"})
        out = m2._run_rollup_stage("fakerepo", "next")
        self.assertTrue(out["ran"])
        self.assertEqual(len(calls), 1)
        args, kwargs = calls[0]
        self.assertEqual(args[:3], ("fakerepo", "next", "base"))
        # The strictly larger set names the older done op.
        self.assertEqual(kwargs["supersedes"],
                         {"op": "rollup-next-base-aaa",
                          "new": ["leaf-3"]})

    def test_n1_reshaped_lane_skips_with_owner_note(self):
        # N1: swapped leaves are neither equal nor a superset — no
        # parallel rollup; the note names the standing op.
        self.done_op("rollup-next-base-aaa", ["leaf-1", "leaf-2"])
        m2._rollup_plan = lambda repo, lane: ("base",
                                              ["leaf-2", "leaf-3"])
        calls = []
        batches.rollup_leaves = lambda *a, **k: (
            calls.append((a, k)) or {})
        out = m2._run_rollup_stage("fakerepo", "next")
        self.assertFalse(out["ran"])
        self.assertEqual(out["reason"], "레인 구성이 바뀜")
        self.assertEqual(calls, [])
        stash = sched.load("fakerepo").get("m2_rollup_reshaped") or {}
        self.assertEqual(stash.get("op"), "rollup-next-base-aaa")
        self.assertEqual(stash.get("lane"), "next")

    def test_n1_older_subset_newer_reshaped_skips(self):
        # N1 gap: only the NEWEST done rollup may be superseded. An
        # older strict subset must not spawn a competing rollup while
        # the newest is reshaped.
        self.done_op("rollup-next-old", ["leaf-1"],
                     updated_at="2026-10-20T00:00:00+00:00")
        self.done_op("rollup-next-new", ["leaf-1", "leaf-2"],
                     updated_at="2026-10-21T00:00:00+00:00")
        m2._rollup_plan = lambda repo, lane: ("base",
                                              ["leaf-1", "leaf-3"])
        calls = []
        batches.rollup_leaves = lambda *a, **k: (
            calls.append((a, k)) or {})
        out = m2._run_rollup_stage("fakerepo", "next")
        self.assertFalse(out["ran"])
        self.assertEqual(out["reason"], "레인 구성이 바뀜")
        self.assertEqual(calls, [])
        stash = sched.load("fakerepo").get("m2_rollup_reshaped") or {}
        self.assertEqual(stash.get("op"), "rollup-next-new")

    def test_n1_same_set_clears_stash(self):
        self.done_op("rollup-next-base-aaa", ["leaf-1", "leaf-2"])
        with sched.locked("fakerepo"):
            st = sched.load("fakerepo")
            st["m2_rollup_reshaped"] = {"lane": "next",
                                       "op": "rollup-next-base-aaa",
                                       "at": "2026-10-21T00:00:00+00:00"}
            sched.save(st)
        m2._rollup_plan = lambda repo, lane: ("base",
                                              ["leaf-1", "leaf-2"])
        out = m2._run_rollup_stage("fakerepo", "next")
        self.assertEqual(out["reason"], "already rolled")
        self.assertNotIn("m2_rollup_reshaped",
                         sched.load("fakerepo"))

    def test_no_ready_leaves_is_quiet_skip(self):
        m2._rollup_plan = lambda repo, lane: None
        out = m2._run_rollup_stage("fakerepo", "next")
        self.assertFalse(out["ran"])
        self.assertEqual(out["reason"], "no READY leaves")

    def test_stage_off_is_quiet(self):
        m2.set_policy("fakerepo", "rollup", False)
        out = m2._run_rollup_stage("fakerepo", "next")
        self.assertFalse(out["ran"])

    def test_refusal_blocks_lane_with_message(self):
        m2._rollup_plan = lambda repo, lane: ("base", ["leaf-1"])

        def refuse(*a, **k):
            raise batches.IntegrationError("합치기 막힘: 가짜")
        batches.rollup_leaves = refuse
        out = m2._run_rollup_stage("fakerepo", "next")
        self.assertFalse(out["ran"])
        self.assertIn("가짜", out["reason"])

    def test_oplock_busy_is_skip_never_block(self):
        m2._rollup_plan = lambda repo, lane: ("base", ["leaf-1"])

        def busy(*a, **k):
            raise batches.IntegrationError(
                "다른 확인 작업이 rollup-next-base-x에서 실행 중입니다",
                409)
        batches.rollup_leaves = busy
        out = m2._run_rollup_stage("fakerepo", "next")
        self.assertFalse(out["ran"])
        self.assertEqual(out["reason"], "다른 확인 작업 실행 중")


class RollupSafetyTests(unittest.TestCase):
    def test_no_push_in_git_calls_on_rollup_path(self):
        # The "git push" lines in batches.py are owner-command TEXT
        # for copy-paste; no _run_git call may ever push.
        import re
        src = Path(batches.__file__).read_text(encoding="utf-8")
        calls = re.findall(r"_run_git\(\[([^\]]*)\]", src)
        self.assertTrue(calls)
        for argv in calls:
            self.assertNotIn('"push"', argv)
            self.assertNotIn("'push'", argv)


if __name__ == "__main__":
    unittest.main()
