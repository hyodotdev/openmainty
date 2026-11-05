"""Slice 288a: DEMO.md section 4 matches the 285b/286/287c screens.

- The glance bullet names the "today's decisions" hero heading and the
  read-only judgement ledger, not the old repo cards.
- The command bullet names the "things needing answers" heading, not the
  old "needs decision" wording.
- The digest-empty bullet names its h1.
"""
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DEMO = BASE / "docs" / "DEMO.md"


def _section4(text):
    start = text.index("## 4.")
    end = text.index("## 5.", start)
    return text[start:end]


def _bullet(sec4, marker):
    lines = sec4.splitlines()
    start = next(i for i, ln in enumerate(lines)
                 if ln.startswith(marker))
    block = [lines[start]]
    for ln in lines[start + 1:]:
        if ln.startswith("- "):
            break
        block.append(ln)
    return " ".join("\n".join(block).split())


class DemoRefreshCase(unittest.TestCase):
    def test_glance_names_hero_and_ledger(self):
        bullet = _bullet(_section4(DEMO.read_text(encoding="utf-8")),
                         "- 한눈에 보기")
        self.assertIn("오늘 결정할 일", bullet)
        self.assertIn("읽기 전용 판단", bullet)
        self.assertIn("목록 너비 조절", bullet)
        self.assertNotIn("오늘의 읽기 전용 판단", bullet)
        self.assertNotIn("카드와", bullet)

    def test_command_names_answer_heading(self):
        bullet = _bullet(_section4(DEMO.read_text(encoding="utf-8")),
                         "- 관제센터")
        self.assertIn("답변이 필요한 일", bullet)
        self.assertNotIn("결정 필요", bullet)

    def test_digest_empty_names_h1(self):
        bullet = _bullet(_section4(DEMO.read_text(encoding="utf-8")),
                         "- 하루 요약")
        self.assertIn('"하루 요약"', bullet)
        self.assertIn("아직 ", bullet)


if __name__ == "__main__":
    unittest.main()
