"""Slice 284: DEMO.md section 4 quotes what the app really shows.

- Every literal Korean UI string quoted in section 4 occurs in the
  product: dashboard.html for template text, demo.py for seed-rendered
  strings (e.g. the run-demomiss01 title). Dynamic placeholders in
  <angle brackets> are skipped; only the fixed parts are checked.
- Section 4 must not contain "still no runs" wording that no longer
  exists in the source; the empty repo is described by its route and
  the "checkout not linked" notice.
- Section 4 must not describe the demo glance as having a blocked item.
"""
import re
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DEMO = BASE / "docs" / "DEMO.md"
DASH = BASE / "src" / "openmainty" / "dashboard.html"
SEED = BASE / "src" / "openmainty" / "demo.py"

QUOTE_RE = re.compile(r'"([^"]+)"')
PLACEHOLDER_RE = re.compile(r"<[^<>]+>")


def _normalize(quoted):
    return " ".join(quoted.split())


def _section4(text):
    start = text.index("## 4.")
    end = text.index("## 5.", start)
    return text[start:end]


def _glance_bullet(sec4):
    lines = sec4.splitlines()
    start = next(i for i, ln in enumerate(lines)
                 if ln.startswith("- 한눈에 보기"))
    block = [lines[start]]
    for ln in lines[start + 1:]:
        if ln.startswith("- "):
            break
        block.append(ln)
    return "\n".join(block)


class DemoExpectationsCase(unittest.TestCase):
    def test_quoted_strings_are_real_product_text(self):
        sec4 = _section4(DEMO.read_text(encoding="utf-8"))
        product = (DASH.read_text(encoding="utf-8") + "\n"
                   + SEED.read_text(encoding="utf-8"))
        quotes = QUOTE_RE.findall(sec4)
        self.assertTrue(quotes, "no quoted strings in section 4")
        for q in quotes:
            q = _normalize(q)
            parts = [p for p in PLACEHOLDER_RE.split(q) if len(p) >= 2]
            self.assertTrue(parts, "empty quote after placeholders: " + q)
            for part in parts:
                self.assertIn(part, product,
                              "quoted text not in product: " + part)

    def test_empty_repo_uses_route_and_notice(self):
        sec4 = _section4(DEMO.read_text(encoding="utf-8"))
        self.assertNotIn("아직 실행 없음", sec4)
        self.assertIn("체크아웃 미연결", sec4)
        self.assertIn("#/repo/demo-empty-repo?tab=work", sec4)

    def test_glance_has_no_blocked_item(self):
        bullet = _glance_bullet(
            _section4(DEMO.read_text(encoding="utf-8")))
        self.assertNotIn("막힌 일", bullet)


if __name__ == "__main__":
    unittest.main()
