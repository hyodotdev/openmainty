"""Slice 281: the submission package docs stay honest and linked.

- docs/SUBMISSION.md exists with the required sections.
- Every relative markdown link in SUBMISSION.md/HACKATHON.md resolves.
- Every `openmainty <subcommand>` in SUBMISSION.md is a real CLI
  subcommand (parsed from cli.py argparse setup).
- A forbidden-claims guard (Korean + English) passes on the real
  text and fails on a planted phrase in a temp copy.
- The live app id in SUBMISSION.md is 16 hex chars.
"""
import ast
import re
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SUBMISSION = BASE / "docs" / "SUBMISSION.md"

REQUIRED_HEADINGS = [
    "한 줄 소개",
    "지금 실제로 동작하는 것",
    "어떻게 만들었나",
    "데모 재현",
    "남은 보류와 사람이 할 일",
    "하지 않는 주장",
    "English summary",
]

FORBIDDEN = [
    "우승했", "우승 확정", "우승을 확정", "1등 확정",
    "모든 이슈를 해결", "모든 PR을 해결", "모든 논의를 해결",
    "all issues resolved", "all PRs resolved", "we won",
    "guaranteed win", "guaranteed to win",
]

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)#\s]+)(?:#[^)\s]*)?\)")
SUBCMD_RE = re.compile(r"openmainty\s+([a-z][a-z0-9-]*)")
APPID_RE = re.compile(r"^[0-9a-f]{16}$")


def _real_subcommands():
    tree = ast.parse((BASE / "src" / "openmainty" / "cli.py")
                     .read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute)
                and func.attr == "add_parser"):
            continue
        if node.args and isinstance(node.args[0], ast.Constant):
            names.add(node.args[0].value)
    if not names:
        raise AssertionError("no subcommands parsed from cli.py")
    return names


def _relative_links(text):
    out = []
    for m in LINK_RE.finditer(text):
        target = m.group(1)
        if "://" in target or target.startswith(("mailto:", "#")):
            continue
        out.append(target)
    return out


def _forbidden_hits(text):
    lowered = text.lower()
    return [p for p in FORBIDDEN if p.lower() in lowered]


class SubmissionDocsCase(unittest.TestCase):
    def test_submission_has_required_sections(self):
        self.assertTrue(SUBMISSION.is_file(), "docs/SUBMISSION.md missing")
        text = SUBMISSION.read_text(encoding="utf-8")
        for heading in REQUIRED_HEADINGS:
            self.assertIn(heading, text,
                          "missing section: " + heading)

    def test_relative_links_resolve(self):
        for name in ("SUBMISSION.md", "HACKATHON.md"):
            path = BASE / "docs" / name
            text = path.read_text(encoding="utf-8")
            for target in _relative_links(text):
                dest = (path.parent / target).resolve()
                self.assertTrue(
                    dest.is_file(),
                    "%s: broken link %s" % (name, target))

    def test_subcommands_are_real(self):
        text = SUBMISSION.read_text(encoding="utf-8")
        real = _real_subcommands()
        found = set(SUBCMD_RE.findall(text))
        self.assertTrue(found, "no openmainty subcommands cited")
        for sub in sorted(found):
            self.assertIn(sub, real,
                          "phantom subcommand: openmainty " + sub)

    def test_forbidden_claims_guard(self):
        text = SUBMISSION.read_text(encoding="utf-8")
        self.assertEqual(_forbidden_hits(text), [],
                         "forbidden claim present")
        # The guard itself must fire on planted phrases.
        with tempfile.TemporaryDirectory() as tmp:
            planted = Path(tmp) / "planted.md"
            planted.write_text(text + "\n우리는 우승 확정이다.\n",
                               encoding="utf-8")
            bad = planted.read_text(encoding="utf-8")
            self.assertIn("우승 확정", _forbidden_hits(bad),
                          "guard misses planted Korean phrase")
            planted.write_text(text + "\nAnd we won everything.\n",
                               encoding="utf-8")
            bad_en = planted.read_text(encoding="utf-8")
            self.assertIn("we won", _forbidden_hits(bad_en),
                          "guard misses planted English phrase")

    def test_cited_280b_code_id_is_16_hex(self):
        # The 280b code id is cited as a fixed past fact; this docs
        # slice gets its own new id on promotion, so the doc never
        # calls any id the current live id.
        text = SUBMISSION.read_text(encoding="utf-8")
        ids = set(re.findall(r"\b[0-9a-f]{16}\b", text))
        self.assertTrue(ids, "no cited app id found")
        for app_id in sorted(ids):
            self.assertRegex(app_id, APPID_RE)
        self.assertIn("60dcdc324179c93f", ids)


if __name__ == "__main__":
    unittest.main()
