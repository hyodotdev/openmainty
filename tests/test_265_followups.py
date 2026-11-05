"""Gate-3 live-check followups (265): narrow-width node names lose the tail.

At 390px, nodes whose titles share a long prefix ("cold graph job 013")
render as "cold graph job 0..." and are announced with the same truncated
AT name, so siblings are indistinguishable visually and by screen reader.
Cause: the narrow layouts pre-truncate _label (16 chars) and _fullLabel
falls back to that display-fit label.

This file pins the fix with real dashboard functions under node: every
graph node aria-label carries the FULL untruncated name in every mode at
every width; narrow visible labels use middle ellipsis keeping the
distinguishing tail; wide visible output is byte-identical to 263.
Product code extracted verbatim, never hand-mirrored.
"""

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src" / "openmainty" / "dashboard.html"


def _script():
    text = HTML.read_text()
    blocks = re.findall(r"<script>(.*?)</script>", text, re.S)
    assert len(blocks) == 1, "expected one inline dashboard script"
    return blocks[0]


def _balanced(src, i):
    """Balanced-brace body starting at src[i] == '{'."""
    depth, q, line, block, esc = 0, None, False, False, False
    for j in range(i, len(src)):
        c = src[j]
        n = src[j + 1] if j + 1 < len(src) else ""
        if line:
            if c == "\n":
                line = False
            continue
        if block:
            if c == "*" and n == "/":
                block = False
            continue
        if q:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == q:
                q = None
            continue
        if c == "/" and n == "/":
            line = True
            continue
        if c == "/" and n == "*":
            block = True
            continue
        if c in ("'", '"', "`"):
            q = c
            continue
        if c == "{":
            depth += 1
        if c == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    raise AssertionError("unbalanced braces")


def _fn_body(src, name):
    """Balanced-brace body of a top-level (async) function."""
    m = re.search(r"(?:^|\n)(?:async\s+)?function\s+" + re.escape(name)
                  + r"\s*\(", src)
    assert m, f"missing function {name}"
    return _balanced(src, src.index("{", m.start()))


def _decl(src, name):
    m = re.search(
        r"(?:^|\n)((?:async\s+)?function\s+" + re.escape(name)
        + r"\s*\([^)]*\))",
        src,
    )
    assert m, f"missing function {name}"
    return m.group(1) + " " + _fn_body(src, name)


def _run_node(script):
    return subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
    )


L265_FRAG_STUBS = r'''
var graphSel = null, gFocus = null;
var edges = [];
var narrowCol = false, laneSel = null;
var relHl = new Set();
var GFONT = 12;
var PLANNED_BRANCH = new Set();
var GST_KO = {};
var gMode = "overview", ovNarrow = false, panelW = 604;
function typeKo(t) { return String(t || ""); }
function graphColor(t) { return "#888"; }
function esc(s) { return String(s == null ? "" : s); }
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function ok(c, msg) { eq(!!c, true, msg); }
''';


def _fit_block(src):
    """The real per-node label-fit forEach from paintGraph, verbatim."""
    paint = _fn_body(src, "paintGraph")
    key = 'const singleCol = (gMode === "group")'
    assert paint.count(key) == 1, "fit block anchor not unique"
    start = paint.rindex("nodes.forEach", 0, paint.index(key))
    endkey = "const labW = n =>"
    assert paint.count(endkey) == 1, "fit block end anchor not unique"
    return paint[start:paint.index(endkey)]


def _prelabel_block(src):
    """The real label pre-truncation forEach from paintGraph, verbatim."""
    paint = _fn_body(src, "paintGraph")
    key = "n._label = n._labelShort ||"
    assert paint.count(key) == 1, "prelabel anchor not unique"
    start = paint.rindex("nodes.forEach", 0, paint.index(key))
    endkey = "delete n._labelShort;"
    tail = paint.index("});", paint.index(endkey, start)) + len("});")
    return paint[start:tail]


def _node_template(src):
    """The real node <g> map callback from paintGraph, verbatim."""
    paint = _fn_body(src, "paintGraph")
    start = "const fullName = n._fullLabel"
    end = '  }).join("")'
    assert paint.count(start) == 1, "node template anchor not unique"
    i = paint.index(start)
    j = paint.index(end, i)
    return paint[i:j]


def _l265_script(checks, mode="overview", narrow=True, panel=316,
                 need_mid=True):
    src = _script()
    paint = _fn_body(src, "paintGraph")
    m = re.search(r"const fitLabel = \(s, availPx\) => ", paint)
    assert m, "fitLabel arrow missing"
    fit = "const fitLabel = (s, availPx) => " + _balanced(
        paint, paint.index("{", m.start()))
    parts = [L265_FRAG_STUBS,
             _decl(src, "gLabelW"),
             _decl(src, "runNodeTitle"), _decl(src, "gLabel"),
             _decl(src, "branchIsActual"), _decl(src, "branchStateOf"),
             _decl(src, "graphLabelSide")]
    if need_mid:
        parts.append(_decl(src, "fitLabelMid"))
    parts += [fit,
              "var wideRootCap = 0;",
              L265_FIXTURE,
              _prelabel_block(src),
              _fit_block(src),
              "function __node(n) {\n" + _node_template(src) + "\n}",
              checks]
    head = ("gMode = %s; ovNarrow = %s; panelW = %d;"
            "wideRootCap = (gMode === \"overview\" && !ovNarrow) ? 216 : 0;"
            % (repr(mode), "true" if narrow else "false", panel))
    parts.insert(1, head)
    return "\n".join(parts)


L265_FIXTURE = r'''
// Same-prefix siblings: run titles plus dependency upgrade titles.
var NODES = [
  {id: "run:aaa001", type: "run", label: "cold graph job 000", title: "cold graph job 000", status: "done"},
  {id: "run:aaa002", type: "run", label: "cold graph job 013", title: "cold graph job 013", status: "done"},
  {id: "run:aaa003", type: "run", label: "cold graph job 039", title: "cold graph job 039", status: "done"},
  {id: "dep:b1", type: "dependency", label: "@babel/runtime 7.29.7 -> 8.1.0", status: "done"},
  {id: "dep:b2", type: "dependency", label: "@babel/runtime 7.29.7 -> 8.2.0", status: "done"},
  {id: "repo:r1", type: "repo", label: "example-org/sample-repo", status: "done"}
];
var nodes = NODES.map(n => Object.assign({}, n));
// Narrow-tree child column positions (the 390 layout); the real
// prelabel block sets n._label from these.
nodes.forEach((n, i) => {
  if (n.type === "repo") { n._x = 40; n._y = 46; }
  else { n._x = 88; n._y = 94 + i * 62; }
});
'''

L265_CHECKS_AT = r'''
// (a) narrow: every aria-label carries the FULL title; siblings distinct.
var seen = {};
nodes.forEach(n => {
  var g = __node(n);
  var m = g.match(/aria-label="([^"]*)"/);
  ok(m, "node has an aria-label: " + n.id);
  var full = gLabel(n, 10000);
  ok(m[1].indexOf(full) === 0, "aria-label starts with the full name: " + n.id);
  ok(!seen[m[1]], "aria-label distinct: " + m[1]);
  seen[m[1]] = 1;
  var t = g.match(/<title>([^<]*)<\/title>/);
  ok(t && t[1].indexOf(full) === 0, "circle title carries the full name: " + n.id);
});
console.log("PASS " + pass);
'''

L265_CHECKS_TAIL = r'''
// (b) narrow visible labels keep the distinguishing tail.
var tails = {"run:aaa001": "job 000", "run:aaa002": "job 013",
  "run:aaa003": "job 039", "dep:b1": "8.1.0", "dep:b2": "8.2.0"};
var vis = {};
nodes.forEach(n => {
  var want = tails[n.id];
  if (!want) return;
  var lab = n._label || "";
  ok(lab.indexOf(want) >= 0, "visible label keeps tail: " + n.id + " -> " + lab);
  vis[n.id] = lab;
});
var vals = Object.keys(vis).map(k => vis[k]);
ok(new Set(vals).size === vals.length, "visible narrow labels pairwise distinct");
console.log("PASS " + pass);
'''

L265_CHECKS_FIT = r'''
// (d) every fitted label fits its avail; hit rect stays 48px.
nodes.forEach(n => {
  var side = graphLabelSide(n._label || gLabel(n), n._x || 0, panelW, true);
  ok(gLabelW(n._label) <= side.avail, "label fits avail: " + n.id);
  var g = __node(n);
  ok(g.indexOf('width="48" height="48"') >= 0, "48px hit rect: " + n.id);
});
console.log("PASS " + pass);
'''

L265_CHECKS_MID_UNIT = r'''
// fitLabelMid pure units on the real function.
var S = "cold graph job 013";
eq(fitLabelMid(S, 10000), S, "fits whole when there is room");
var mid = fitLabelMid(S, 120);
ok(gLabelW(mid) <= 120, "mid fit respects avail");
ok(mid.indexOf("013") >= 0, "mid fit keeps the tail");
ok(mid.indexOf("\u2026") >= 0, "mid fit marks the cut");
ok(mid.indexOf("cold") === 0, "mid fit keeps the head start");
var cjk = "\uc11c\uc6b8\ub354 \uace0\uc7a5 #774 \ubc30\ud3ec \uc2e4\ud328 \ubcf5\uad6c \uc791\uc5c5";
var cm = fitLabelMid(cjk, 120);
ok(gLabelW(cm) <= 120, "mid fit respects avail for CJK");
eq(fitLabelMid("ab", 100), "ab", "short strings untouched");
console.log("PASS " + pass);
'''


def _l265_golden_outputs(mode, narrow, panel):
    checks = ("var outs = nodes.map(n => [n.id, n._label, n._anchor,"
              " n._x, n._y].join('|'));\n"
              "console.log('GOLDEN ' + JSON.stringify(outs));\n")
    proc = _run_node(_l265_script(checks, mode, narrow, panel,
                                   need_mid=False))
    assert proc.returncode == 0, proc.stderr + proc.stdout
    line = [ln for ln in proc.stdout.splitlines()
            if ln.startswith("GOLDEN ")][0]
    return line[len("GOLDEN "):]


# (c) wide visible output, captured pre-change from the 263 product by the
# muse265-capture throwaway, then frozen. 265 must not move one character
# of visible wide labels, anchors, or positions (aria-labels intentionally
# gain full names; see the at-wide test).
L265_WIDE_OVERVIEW_GOLDEN = ("[\"run:aaa001|cold graph job 000|start|88|94\","
    "\"run:aaa002|cold graph job 013|start|88|156\","
    "\"run:aaa003|cold graph job 039|start|88|218\","
    "\"dep:b1|@babel/runtime 7.29.7 -> 8.1\u2026|start|88|280\","
    "\"dep:b2|@babel/runtime 7.29.7 -> 8.2\u2026|start|88|342\","
    "\"repo:r1|example-org/sample-repo|start|40|46\"]")
L265_WIDE_FOCUS_GOLDEN = ("[\"run:aaa001|cold graph job 000|start|88|94\","
    "\"run:aaa002|cold graph job 013|start|88|156\","
    "\"run:aaa003|cold graph job 039|start|88|218\","
    "\"dep:b1|@babel/runtime 7.29.7 -> 8.1\u2026|start|88|280\","
    "\"dep:b2|@babel/runtime 7.29.7 -> 8.2\u2026|start|88|342\","
    "\"repo:r1|example-org/sample-repo|start|40|46\"]")


class NarrowNamesTest(unittest.TestCase):
    def test_fit_block_uses_full_source_at_narrow(self):
        src = _script()
        paint = _fn_body(src, "paintGraph")
        self.assertIn("fitLabelMid", paint,
                      "narrow middle-ellipsis fit missing from paintGraph")

    def test_full_label_never_from_display_fit(self):
        src = _script()
        paint = _fn_body(src, "paintGraph")
        m = re.search(r"if \(!n\._fullLabel\) n\._fullLabel = ([^;]+);",
                      paint)
        self.assertTrue(m, "_fullLabel fallback assignment missing")
        self.assertNotIn("fitLabel", m.group(1),
                         "_fullLabel must not derive from a display fit")
        self.assertNotEqual(m.group(1).strip(), "full",
                            "_fullLabel must not reuse the truncated label")

    def test_aria_labels_full_and_distinct_narrow(self):
        proc = _run_node(_l265_script(L265_CHECKS_AT,
                                      "overview", True, 316))
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("PASS", proc.stdout)

    def test_visible_labels_keep_tail_narrow(self):
        proc = _run_node(_l265_script(L265_CHECKS_TAIL,
                                      "overview", True, 316))
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("PASS", proc.stdout)

    def test_fit_and_hit_rect_narrow(self):
        proc = _run_node(_l265_script(L265_CHECKS_FIT,
                                      "overview", True, 316))
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("PASS", proc.stdout)

    def test_fit_mid_units(self):
        proc = _run_node(_l265_script(L265_CHECKS_MID_UNIT,
                                      "overview", True, 316))
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("PASS", proc.stdout)

    def test_aria_labels_full_wide(self):
        # Justified wide change: AT names gain full names (req 1 covers
        # every width); visible wide output stays golden-pinned below.
        proc = _run_node(_l265_script(L265_CHECKS_AT,
                                      "overview", False, 604))
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("PASS", proc.stdout)

    def test_wide_goldens_unchanged(self):
        self.assertTrue(L265_WIDE_OVERVIEW_GOLDEN, "overview golden missing")
        self.assertTrue(L265_WIDE_FOCUS_GOLDEN, "focus golden missing")
        self.assertEqual(
            _l265_golden_outputs("overview", False, 604),
            L265_WIDE_OVERVIEW_GOLDEN)
        self.assertEqual(
            _l265_golden_outputs("focus", False, 604),
            L265_WIDE_FOCUS_GOLDEN)


if __name__ == "__main__":
    unittest.main()
