"""Gate-3c live-check followups (247): L11 large-search paging,
L9 focus after clear, L12 legend contrast.

Same strategy as tests/test_245_followups.py: real dashboard
functions extracted and executed under node with stub DOM; constants
that guard a guarantee are extracted verbatim from the shipped
source, never hand-mirrored.
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


def _fn_body(src, name):
    """Balanced-brace body of a top-level (async) function."""
    m = re.search(r"(?:^|\n)(?:async\s+)?function\s+" + re.escape(name)
                  + r"\s*\(", src)
    assert m, f"missing function {name}"
    i = src.index("{", m.start())
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
    raise AssertionError(f"unbalanced function {name}")


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


L11_STUBS = r'''
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function ok(c, msg) { eq(!!c, true, msg); }
function mkHits(n) {
  const out = [];
  for (let i = 0; i < n; i++) out.push({id: "run:r" + i, label: "nitro job " + i});
  return out;
}
function mkNbrs(n) {
  const out = [];
  for (let i = 0; i < n; i++) out.push({id: "issue:e/r#" + i, label: "issue " + i});
  return out;
}
'''

L11_CHECKS = r'''
const HITS = mkHits(250), NBRS = mkNbrs(100);
// A) first page: per-page hits, honest total and range in the note.
const p0 = graphSearchPage("nitro", HITS, NBRS, 0, PER_PAGE);
eq(p0.shown.length, PER_PAGE, "first page is full");
eq(p0.total, 350, "total counts hits plus neighbours");
ok(p0.shown.every(n => n.id.indexOf("run:") === 0),
  "hits come before neighbours");
ok(p0.foldedNote.indexOf("350") !== -1, "note shows the honest total");
ok(p0.foldedNote.indexOf("1") !== -1, "note shows the range start");
ok(p0.modeSub.indexOf("350") !== -1, "sub shows the honest total");
// B) last page bounds and clamping.
eq(p0.pages, Math.ceil(350 / PER_PAGE), "page count covers the total");
const plast = graphSearchPage("nitro", HITS, NBRS, 999, PER_PAGE);
eq(plast.page, p0.pages - 1, "overflow page clamps to the last page");
eq(plast.from, (p0.pages - 1) * PER_PAGE + 1, "last range starts right");
eq(plast.to, 350, "last range ends at the total");
eq(plast.shown.length, 350 - plast.from + 1, "last page holds the rest");
// C) small sets keep today's exact single-page notes (no pager).
const small = graphSearchPage("demo", mkHits(3), mkNbrs(2), 0, PER_PAGE);
eq(small.pages, 1, "small set is one page");
eq(small.shown.length, 5, "small set shows everything");
eq(small.foldedNote, '검색 "demo" 3건 + 이웃', "single-page note unchanged");
console.log("PASS " + pass);
''';


class L11SearchPagingTest(unittest.TestCase):
    # Live "nitro": 253 hits + neighbours = 351 nodes in ONE column
    # (viewBox h 21798) squeezed by the 4000px svg cap to scale 0.18:
    # 8.8px hit rects, ~2.2px labels. Search results page at
    # SEARCH_PAGE_N nodes; the worst single-column viewBox then stays
    # under the cap, so the render scale is 1 at every state.

    def _per_page(self, src):
        m = re.search(r"const SEARCH_PAGE_N = (\d+);", src)
        assert m, "missing const SEARCH_PAGE_N"
        return int(m.group(1))

    def test_paging_is_honest(self):
        src = _script()
        script = (L11_STUBS + "\n" + _decl(src, "graphSearchPage")
                  + f"\nconst PER_PAGE = {self._per_page(src)};\n"
                  + L11_CHECKS)
        proc = _run_node(script)
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout, f"L11 failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")

    def test_rendered_scale_cannot_shrink_hits_or_labels(self):
        # Every constant below is extracted verbatim from the shipped
        # source. Worst case: single-column layout (narrow tree),
        # ROWH per node plus per-repo paddings (bounded by ROWH+8 each)
        # plus fit pads; the svg height cap is the only shrinker.
        src = _script()
        per_page = self._per_page(src)
        m = re.search(r"const COLW = (\d+), ROWH = (\d+);", src)
        assert m, "missing COLW/ROWH line"
        rowh = int(m.group(2))
        m = re.search(r'width="(\d+)" height="(\d+)" fill="transparent"',
                      src)
        assert m, "missing hit rect template"
        hit = int(m.group(1))
        self.assertEqual(hit, int(m.group(2)), "hit rect stays square")
        m = re.search(r"const GFONT = (\d+);", src)
        assert m, "missing GFONT"
        font = int(m.group(1))
        m = re.search(r"svg\.style\.height = Math\.min\((\d+),", src)
        assert m, "missing svg height cap"
        cap = int(m.group(1))
        vb_worst = 200 + per_page * (rowh + 8)
        scale = min(1.0, cap / vb_worst)
        self.assertGreaterEqual(
            hit * scale, 44,
            f"worst-case hit {hit * scale:.1f}px below 44 "
            f"(perPage={per_page} vbH={vb_worst} cap={cap})")
        self.assertGreaterEqual(
            font * scale, 11,
            f"worst-case label {font * scale:.1f}px below 11")

    def test_pager_wiring(self):
        src = _script()
        paint = _fn_body(src, "paintGraph")
        self.assertIn("graphSearchPage(gQuery", paint,
                      "search branch pages through the real helper")
        for btn in ("g-prevpg", "g-nextpg"):
            self.assertIn(f'id="{btn}"', src,
                          f"pager button {btn} exists")
        view = _fn_body(src, "renderGraphView")
        self.assertIn("gSearchPage = 0", view,
                      "query change resets the page")


L9_STUBS = r'''
var gQuery = "old", gSearchPage = 3;
var gClientType = "run", gClientStatus = "x";
var __focused = [];
function __el(id) {
  return {value: "x", focus: function (o) { __focused.push(id); }};
}
var __els = {};
function $(id) {
  if (!__els[id]) __els[id] = __el(id);
  return __els[id];
}
function syncHash() {}
function paintGraph() {}
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function ok(c, msg) { eq(!!c, true, msg); }
''';

L9_CHECKS = r'''
clearGraphSearch();
eq(gQuery, "", "query cleared");
eq($("g-q").value, "", "search box emptied");
eq(gSearchPage, 0, "page reset");
eq(__focused[__focused.length - 1], "g-q",
  "clear search moves focus to the search box");
clearGraphFilters();
eq(gClientType, "", "type cleared");
eq($("gf-type").value, "", "type select emptied");
eq(__focused[__focused.length - 1], "gf-type",
  "clear filters moves focus to the type select");
console.log("PASS " + pass);
''';


class L9ClearFocusTest(unittest.TestCase):
    # After "검색 지우기" (or filter clear) the panel repaints and
    # removes the button, dropping focus to BODY. Focus must move to
    # the graph search box (filters: the type select) instead.

    def test_clear_moves_focus(self):
        src = _script()
        script = (L9_STUBS + "\n" + _decl(src, "clearGraphSearch")
                  + "\n" + _decl(src, "clearGraphFilters")
                  + "\n" + L9_CHECKS)
        proc = _run_node(script)
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout, f"L9 failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")


L12_STUBS = r'''
var legend = {innerHTML: ""};
var gClientType = "";
const TYPE_KO = {repo: "저장소", work: "대기 작업", run: "실행",
  verification: "검증", branch: "브랜치", issue: "이슈 참조",
  question: "질문", dependency: "의존성"};
function esc(s) { return String(s == null ? "" : s); }
function graphColor(t) { return "#000000"; }
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function ok(c, msg) { eq(!!c, true, msg); }
''';

L12_CHECKS = r'''
__renderLegend();
ok(legend.innerHTML.indexOf('style="color:') === -1,
  "no inline low-contrast glyph colour");
const types = ["repo", "work", "run", "verification", "branch",
  "issue", "question", "dependency"];
types.forEach(t => {
  ok(legend.innerHTML.indexOf('data-t="' + t + '"') !== -1,
    "swatch carries " + t);
});
console.log("PASS " + pass);
''';


def _lum(hex6):
    c = [int(hex6[i:i + 2], 16) / 255 for i in (1, 3, 5)]

    def lin(x):
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4

    c = [lin(x) for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def _ratio(a, b):
    x, y = sorted([_lum(a), _lum(b)], reverse=True)
    return (x + 0.05) / (y + 0.05)


class L12LegendContrastTest(unittest.TestCase):
    # Legend "●" glyphs are meaningful graphics (WCAG 1.4.11, >=3:1).
    # The dark-theme node colours fail on the light card (2.13-3.35),
    # so legend swatches use per-theme colours; SVG nodes are untouched.

    def test_swatch_uses_theme_class(self):
        paint = _fn_body(_script(), "paintGraph")
        anchor = "legend.innerHTML = Object.entries(TYPE_KO)"
        self.assertEqual(paint.count(anchor), 1, "legend anchor unique")
        start = paint.index(anchor)
        end = paint.index('.join("");', start) + len('.join("");')
        frag = paint[start:end]
        script = (L12_STUBS + "\nfunction __renderLegend() {\n"
                  + frag + "\n}\n" + L12_CHECKS)
        proc = _run_node(script)
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout, f"L12 failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")

    def test_swatch_colours_hit_3_to_1_both_themes(self):
        css = HTML.read_text()
        light = dict(re.findall(
            r'^\.legend \.sw\[data-t="(\w+)"\] \{ color: (#[0-9a-f]{6}); \}',
            css, re.M))
        dark = dict(re.findall(
            r'html\[data-theme="dark"\] \.legend \.sw\[data-t="(\w+)"\]'
            r" \{ color: (#[0-9a-f]{6}); \}",
            css))
        want = {"repo", "work", "run", "verification", "branch",
                "issue", "question", "dependency"}
        self.assertEqual(set(light), want, "light swatch set")
        self.assertEqual(set(dark), want, "dark swatch set")
        self.assertNotEqual(light, dark,
                            "light and dark swatch sets must differ")
        m = re.search(r":root\s*\{[^}]*?--panel:\s*(#[0-9a-f]{6})", css)
        assert m, "light panel bg missing"
        light_bg = m.group(1)
        m = re.search(r'html\[data-theme="dark"\]\s*\{[^}]*?'
                      r"--panel:\s*(#[0-9a-f]{6})", css)
        assert m, "dark panel bg missing"
        dark_bg = m.group(1)
        for t in sorted(want):
            with self.subTest(type=t):
                self.assertGreaterEqual(
                    _ratio(light[t], light_bg), 3.0,
                    f"light {t} {light[t]} on {light_bg}")
                self.assertGreaterEqual(
                    _ratio(dark[t], dark_bg), 3.0,
                    f"dark {t} {dark[t]} on {dark_bg}")


if __name__ == "__main__":
    unittest.main()
