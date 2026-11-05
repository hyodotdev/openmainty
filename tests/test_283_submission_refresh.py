"""Slice 283: SUBMISSION.md structural pins (gate wording, no 610994d7
in the gate-2 block).

The 282-era value pins (promoted id af255d70a7091822, the v2 evidence
index, 116 modules / 3587 tests) were retired in slice 288b: the
document now describes the promoted 287c state, and
tests/test_288b_submission_refresh.py pins the new values. The
structural pins below stay because the gate wording they guard is
unchanged.
"""
import re
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SUBMISSION = BASE / "docs" / "SUBMISSION.md"


def _gate2_block(text):
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if "gate 2" in ln)
    block = [lines[start]]
    for ln in lines[start + 1:]:
        if ln.startswith("- ") or ln.startswith("## "):
            break
        block.append(ln)
    return "\n".join(block)


class SubmissionRefreshCase(unittest.TestCase):
    def test_gate3_pass_gates12_open(self):
        text = SUBMISSION.read_text(encoding="utf-8")
        self.assertRegex(text, r"gate 3[^\n]*통과")
        self.assertIn("gate 1 (열림)", text)
        self.assertIn("gate 2 (열림)", text)

    def test_plan_chat_not_gate2_evidence(self):
        text = SUBMISSION.read_text(encoding="utf-8")
        self.assertNotIn("610994d7", _gate2_block(text))


if __name__ == "__main__":
    unittest.main()
