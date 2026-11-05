"""Slice 292, STAGED: the #/command header status line clips at 390px.

CSS pins (dashboard.html <style> only, no behaviour change): a
.cmd-f value wraps inside the line when it does not fit, the
field is bounded by the line width, and the short label stays on
one line next to it.
"""
import re
import unittest
from pathlib import Path


def _html():
    base = Path(__file__).resolve().parent.parent
    return (base / "src" / "openmainty" / "dashboard.html").read_text(
        encoding="utf-8")


def _rule(html, selector):
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", html)
    if not m:
        raise AssertionError("missing CSS rule " + selector)
    return m.group(1)


class CmdlineWrap292Case(unittest.TestCase):
    def test_value_can_wrap(self):
        body = _rule(_html(), ".cmd-f span:last-child")
        self.assertIn("white-space: normal", body)
        self.assertIn("overflow-wrap: anywhere", body)

    def test_field_bounded_by_line(self):
        body = _rule(_html(), ".cmd-f")
        self.assertIn("max-width: 100%", body)
        self.assertIn("min-width: 0", body)
        self.assertNotIn("white-space: nowrap", body)

    def test_label_stays_one_line(self):
        body = _rule(_html(), ".cmd-f .k")
        self.assertIn("white-space: nowrap", body)
