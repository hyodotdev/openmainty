"""Slice 288b: SUBMISSION.md structural pins (gate-3 wording, fixed
notes, replay citation, 282 not latest).

The 287c-era value pins (promoted id 0215fe6129da8c84, the v3
evidence index, 121 modules / 3627 tests, 27 promotions
240 -> 287c, the 135036Z tracking refresh) were retired in slice
290: the document now describes the promoted 289b state, and
tests/test_290_submission_refresh.py pins the new values. The
288a demo-replay citation stays because DEMO.md has not changed
since 288a. The structural pins below stay because the gate
wording they guard is unchanged.
"""
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SUBMISSION = BASE / "docs" / "SUBMISSION.md"

REPLAY_288A = ("Root-Claude-288a-staged-review-and-gate5-demo-replay-"
               "PASS-20261104T140920Z.json")


class Submission288bCase(unittest.TestCase):
    def test_cites_replay(self):
        text = SUBMISSION.read_text(encoding="utf-8")
        self.assertIn(REPLAY_288A, text)

    def test_282_not_latest(self):
        text = SUBMISSION.read_text(encoding="utf-8")
        self.assertNotIn("마지막 코드 슬라이스(282", text)

    def test_gate3_scope_is_checked_routes(self):
        text = " ".join(SUBMISSION.read_text(encoding="utf-8").split())
        self.assertNotIn("모든 기준을 통과", text)
        self.assertIn("확인된 경로에서 통과", text)

    def test_fixed_notes_not_open(self):
        text = " ".join(SUBMISSION.read_text(encoding="utf-8").split())
        self.assertNotIn("실패 시 최대 3회", text)
        self.assertNotIn("부팅 시 중복 요약 조회(기존)", text)
        self.assertIn("Root-Claude-285b-staged-review-PASS-"
                      "not-promoted-20261104T040501Z.json", text)
        self.assertIn("L8/L10", text)


if __name__ == "__main__":
    unittest.main()
