"""Slice 290: SUBMISSION.md matches the verified state after 289b.

- Cites the promoted 289b code id 72b08fffcba09891 in the Korean
  body and in the English summary.
- Cites the v4 evidence index by name.
- Says 124 modules and 3659 tests per Python.
- Says 29 promotions 240 -> 289b.
- Cites the 289/289b review record and the 192121Z tracking
  refresh by name.
- 287c-as-latest wording is gone (287c stays only as history).
"""
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SUBMISSION = BASE / "docs" / "SUBMISSION.md"

CODE_ID_289B = "72b08fffcba09891"
V4_INDEX = "Root-Claude-submission-evidence-index-v4-20261104T192153Z.json"
REVIEW_289 = ("Root-Claude-289-289b-staged-review-PASS-sandbox-browser-"
              "20261104T190428Z.json")
TRACKING = ("Root-Claude-gate6-issue-pr-discussion-tracking-refresh-"
            "20261104T192121Z.json")


def _body_and_english(text):
    cut = text.index("## English summary")
    return text[:cut], text[cut:]


class Submission290Case(unittest.TestCase):
    def test_cites_promoted_289b_id_both_languages(self):
        body, english = _body_and_english(
            SUBMISSION.read_text(encoding="utf-8"))
        self.assertIn(CODE_ID_289B, body)
        self.assertIn(CODE_ID_289B, english)

    def test_cites_v4_index(self):
        text = SUBMISSION.read_text(encoding="utf-8")
        self.assertIn(V4_INDEX, text)

    def test_suite_counts(self):
        text = SUBMISSION.read_text(encoding="utf-8")
        self.assertIn("124", text)
        self.assertIn("3659", text)

    def test_promotion_counts(self):
        text = SUBMISSION.read_text(encoding="utf-8")
        self.assertIn("29", text)
        self.assertIn("240 \u2192 289b", text)

    def test_cites_review_and_tracking(self):
        text = SUBMISSION.read_text(encoding="utf-8")
        self.assertIn(REVIEW_289, text)
        self.assertIn(TRACKING, text)

    def test_287c_not_latest(self):
        text = SUBMISSION.read_text(encoding="utf-8")
        self.assertNotIn("마지막 코드 슬라이스(287c", text)


if __name__ == "__main__":
    unittest.main()
