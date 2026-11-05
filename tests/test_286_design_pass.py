"""Slice 286: production-ready visual design pass (staging only).

Splitter (shared by sidebar + decision desk): keyboard, clamp,
remembered width with throwing storage still rendering.
Decision detail: no empty section headings, one primary action.
Header: compact status chip with full text for AT.
CSS pins: one radius set, no Arial leak, 44px buttons.
"""
import re
import subprocess
import unittest
from pathlib import Path

NODE = "node"


def _src():
    base = Path(__file__).resolve().parent.parent
    return (base / "src" / "openmainty" / "dashboard.html").read_text(
        encoding="utf-8")


def _decl(src, name):
    for prefix in ("async function " + name + "(",
                   "function " + name + "("):
        i = src.find(prefix)
        if i != -1:
            break
    else:
        raise AssertionError("missing function " + name)
    j = src.index("{", i)
    depth = 0
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    raise AssertionError("unbalanced " + name)


def _run_node(prog):
    return subprocess.run(
        [NODE, "-e", prog], capture_output=True, text=True, timeout=60)


def _node_ok(prog):
    cp = _run_node(prog)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    return cp.stdout


OK_STUB = r'''
var pass = 0;
function ok(c, name) {
  if (!c) { console.log("FAIL " + name); process.exitCode = 1; }
  else pass++;
}
'''


class SplitClampTest(unittest.TestCase):
    def test_clamp(self):
        src = _src()
        prog = OK_STUB + _decl(src, "splitClamp") + r'''
ok(splitClamp(50, 100, 400) === 100, "floor");
ok(splitClamp(500, 100, 400) === 400, "ceil");
ok(splitClamp(200, 100, 400) === 200, "inside");
ok(splitClamp(NaN, 100, 400) === 100, "nan-floor");
console.log("pass=" + pass);
'''
        out = _node_ok(prog)
        self.assertIn("pass=4", out)


class SplitKeyTest(unittest.TestCase):
    def test_keys(self):
        src = _src()
        prog = OK_STUB + _decl(src, "splitClamp") + \
            _decl(src, "splitKey") + r'''
var st = {value: 200, min: 100, max: 400, step: 20};
ok(splitKey("ArrowLeft", st) === 180, "left");
ok(splitKey("ArrowRight", st) === 220, "right");
ok(splitKey("Home", st) === 100, "home");
ok(splitKey("End", st) === 400, "end");
ok(splitKey("ArrowLeft",
  {value: 100, min: 100, max: 400, step: 20}) === 100, "left-edge");
ok(splitKey("ArrowRight",
  {value: 400, min: 100, max: 400, step: 20}) === 400, "right-edge");
ok(splitKey("a", st) === null, "other-null");
console.log("pass=" + pass);
'''
        out = _node_ok(prog)
        self.assertIn("pass=7", out)


class SplitStoreTest(unittest.TestCase):
    def test_throwing_storage_renders(self):
        src = _src()
        prog = OK_STUB + _decl(src, "splitRemember") + \
            _decl(src, "splitRecall") + r'''
var store = {};
var ls = {getItem: function(k) { return (k in store) ? store[k] : null; },
          setItem: function(k, v) { store[k] = String(v); }};
splitRemember("d", 220, ls);
ok(splitRecall("d", 300, ls) === 220, "roundtrip");
ok(splitRecall("x", 300, ls) === 300, "fallback");
var bad = {getItem: function() { throw new Error("deny"); },
           setItem: function() { throw new Error("deny"); }};
ok(splitRecall("d", 300, bad) === 300, "throw-recall");
splitRemember("d", 220, bad);
ok(true, "throw-remember");
console.log("pass=" + pass);
'''
        out = _node_ok(prog)
        self.assertIn("pass=4", out)


class DeskSecTest(unittest.TestCase):
    def test_no_empty_heading(self):
        src = _src()
        prog = OK_STUB + _decl(src, "deskSec") + \
            _decl(src, "esc") + r'''
ok(deskSec("왜 지금인가", "") === "", "empty");
ok(deskSec("왜 지금인가", "   ") === "", "spaces");
ok(deskSec("왜 지금인가", "<div></div>") === "", "empty-tags");
var h = deskSec("왜 지금인가", "<div>이유</div>");
ok(h.indexOf("왜 지금인가") !== -1 && h.indexOf("이유") !== -1,
   "kept");
console.log("pass=" + pass);
'''
        out = _node_ok(prog)
        self.assertIn("pass=4", out)


class StatusChipTest(unittest.TestCase):
    def test_full_text_for_at(self):
        src = _src()
        prog = OK_STUB + _decl(src, "statusChip") + \
            _decl(src, "esc") + r'''
var h = statusChip("spark-1.3 · 구독 요청됨 · 미검증", "미검증");
ok(h.indexOf('title="spark-1.3 · 구독 요청됨 · 미검증"') !== -1,
   "title");
ok(h.indexOf("aria-label") === -1, "no-aria-label");
ok(h.indexOf('<span class="vh">spark-1.3 · 구독 요청됨 · 미검증</span>') !== -1,
   "vh-full");
ok(h.indexOf('aria-hidden="true">미검증<') !== -1, "hidden-short");
var x = statusChip('a"b', "c");
ok(x.indexOf("&quot;") !== -1, "escaped");
console.log("pass=" + pass);
'''
        out = _node_ok(prog)
        self.assertIn("pass=5", out)


class IdentityCssTest(unittest.TestCase):
    def test_identity_pins(self):
        src = _src()
        m = re.search(r"<style>(.*?)</style>", src, re.S)
        self.assertIsNotNone(m)
        css = m.group(1)
        self.assertNotIn("Arial", css)
        self.assertIn("--r-lg", css)
        self.assertIn(".split", css)
        self.assertIn("--accent", css)
        self.assertIn(".ledger", css)
        self.assertIn(".hero-title", css)
        self.assertIn(".chip-n", css)
        self.assertIn(".mpdot", css)
        mb = re.search(r"\.btn\s*\{[^}]*min-height:\s*44px", css)
        self.assertIsNotNone(mb)


class DeskSplitMinTest(unittest.TestCase):
    def test_list_minimum_280(self):
        src = _src()
        self.assertIn('aria-valuemin="280"', src)
        self.assertIn("splitClamp(px, 280, 560)", src)
        self.assertIn('min: 280, max: 560', src)
        self.assertIn("setPointerCapture", src)


class FooterDotFreeTest(unittest.TestCase):
    def test_scroll_padding_and_fields(self):
        src = _src()
        m = re.search(r"<style>(.*?)</style>", src, re.S)
        self.assertIsNotNone(m)
        css = m.group(1)
        sp = re.search(r"#view\s*\{[^}]*scroll-padding-top:\s*\d+px",
                       css)
        self.assertIsNotNone(sp)
        self.assertIn("전체 현황과 원문 기록", src)
        self.assertNotIn("전체 현황·원문 기록", src)
        self.assertIn('"개발 트리와 다름"', src)
        self.assertNotIn('" · 개발 트리와 다름"', src)
        self.assertIn('class="k">디스크 여유', src)
        self.assertIn('class="k">앱 크기', src)
        self.assertNotIn("· 디스크 여유", src)
        self.assertIn("hw.textContent = msg;", src)


class SkipScrollTest(unittest.TestCase):
    def test_skip_link_scrolls_target_into_view(self):
        src = _src()
        i = src.index("function skipToView")
        j = src.index("function repoWmenuOpen")
        body = src[i:j]
        self.assertIn("skipScrollTarget()", body)
        self.assertIn('scrollIntoView({block: "start"})', src[i:])
        self.assertIn("preventDefault", body)


if __name__ == "__main__":
    unittest.main()