"""Gate-3 live-check followups (259): graph state and honesty.

L10f-stale-sel (a): a selection that is not among the displayed
nodes must never scope lanes or hints; focus falls back to the
center. L10f-hash-all (b): a modeless hash leaves mode=all for the
overview. L10h (c, d): /api/graph reports run truncation, the graph
chrome discloses it once, and subtitles never claim "all" when the
graph is cut. Esc focus (e): leaving focus returns keyboard focus
to the origin node when still displayed, else the canvas. (f) pins
the 253/255/257 goldens. Real dashboard functions under node and
real graph.py in isolated homes; product constants extracted
verbatim, never hand-mirrored.
"""

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src" / "openmainty" / "dashboard.html"

sys.path.insert(0, str(ROOT / "src"))

from openmainty import graph, repos, store  # noqa: E402

try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402


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


L10F_STUBS = r'''
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function ok(c, msg) { eq(!!c, true, msg); }
''';

L10F_CHECKS_A = r'''
// (a) a stale selection voids; a displayed one keeps scoping.
var ANODES = [{id: "issue:C"}, {id: "run:N0"}, {id: "run:N1"}];
eq(validGraphSel("queue:STALE", ANODES), null, "stale selection voids");
eq(validGraphSel("run:N0", ANODES), "run:N0", "displayed keeps");
eq(validGraphSel(null, ANODES), null, "no selection stays null");
eq(validGraphSel("run:N0", []), null, "empty view voids");
eq(validGraphSel("run:N0", null), null, "missing view voids");
// The fallback chain: a voided selection in focus scopes the
// center, so the center relations draw.
var scA = laneScope(false, false, "focus", true,
  validGraphSel("queue:STALE", ANODES), "issue:C", false);
eq(scA.col, true, "focus still gates");
eq(scA.sel, "issue:C", "stale falls back to the center");
var scA2 = laneScope(false, false, "focus", true,
  validGraphSel("run:N0", ANODES), "issue:C", false);
eq(scA2.sel, "run:N0", "displayed selection still scopes");
console.log("PASS " + pass);
''';

L10F_HASH_STUBS = r'''
var location = {hash: ""};
var gClientType = "", gClientStatus = "";
var gMode = "overview", gFocus = null, graphSel = null;
var gQuery = "", gNeedFit = false;
var gDetailKey = null, gZeroNotice = false;
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function ok(c, msg) { eq(!!c, true, msg); }
''';

L10F_CHECKS_AREPRO = r'''
// The L10f-stale-sel repro: focused on queue Q11, then a mode-ful
// hash to hub #19 with q. The hash keeps the selection (255 (c));
// paint must void it because Q11 is not in the hub view.
graphSel = "queue:Q11"; gMode = "focus"; gFocus = "queue:Q11";
gQuery = "";
location.hash = "#/graph?mode=focus&focus=issue%3AH%2319&q=issue";
var av = parseHash();
applyGraphHashState(av);
eq(gMode, "focus", "mode-ful hash applies");
eq(gFocus, "issue:H#19", "new focus applies");
eq(graphSel, "queue:Q11", "hash keeps the selection");
var NEWN = [{id: "issue:H#19"}, {id: "run:R1"}, {id: "run:R2"}];
var effA = validGraphSel(graphSel, NEWN);
eq(effA, null, "stale queue voids in the hub view");
var scR = laneScope(false, false, "focus", true, effA, gFocus, true);
eq(scR.col, true, "hub view gates");
eq(scR.sel, "issue:H#19", "lanes fall back to the new center");
console.log("PASS " + pass);
''';

L10F_CHECKS_B = r'''
// (b) a modeless hash leaves mode=all for the overview.
gMode = "all"; gFocus = null; graphSel = "S"; gQuery = "";
gDetailKey = "x";
location.hash = "#/graph";
var bv = parseHash();
applyGraphHashState(bv);
eq(gMode, "overview", "modeless hash leaves all");
eq(gFocus, null, "focus cleared");
eq(graphSel, null, "selection cleared");
// A mode-ful all hash (the toggle path) keeps all.
gMode = "overview"; gFocus = null; graphSel = null; gQuery = "";
location.hash = "#/graph?mode=all";
bv = parseHash();
applyGraphHashState(bv);
eq(gMode, "all", "mode-ful all keeps all");
console.log("PASS " + pass);
''';

L10F_CHECKS_DSUB = r'''
// (d) truncated graphs never claim "all" in the focus subtitle.
eq(focusQuerySub("C", 17, 0, true, 17, "issue", false, true),
  'C — 검색 "issue"에 일치하는 노드라 그래프에 포함된 이웃 17건을 봅니다 (이웃 중 일치 0건)',
  "truncated center-hit names the graph, not all");
eq(focusQuerySub("C", 17, 0, true, 17, "issue", false, false),
  'C — 검색 "issue"에 일치하는 노드라 이웃 17건을 모두 봅니다 (이웃 중 일치 0건)',
  "untruncated center-hit keeps the 255 form");
eq(focusQuerySub("C", 4, 2, false, 2, "run", false, true),
  'C — 이웃 4건 중 검색 "run" 일치 2건과 그 이웃 2건만 봅니다',
  "truncated hit form unchanged");
eq(focusQuerySub("C", 25, 0, true, 25, "hub", true, true),
  'C — 검색 "hub"에 일치하는 노드라 이웃 25건 중 20건을 봅니다 (이웃 중 일치 0건, 일치 먼저)',
  "truncated capped form unchanged");
eq(focusQuerySub("C", 4, 0, false, 4, "zz", false, true),
  'C — 이웃 4건 중 검색 "zz" 일치 없음 · 검색을 지우면 이웃 4건을 봅니다',
  "truncated zero-match form unchanged");
console.log("PASS " + pass);
''';

L10F_CHECKS_DNOTICE = r'''
// (d) truncation detection and the one chrome disclosure.
eq(graphTruncated({runs: {included: 200, total: 312}}), true,
  "cut reports cut");
eq(graphTruncated({runs: {included: 3, total: 3}}), false,
  "whole reports whole");
eq(graphTruncated(null), false, "missing limits is untruncated");
eq(graphTruncated({}), false, "empty limits is untruncated");
eq(graphTruncated({runs: {included: 200}}), false,
  "malformed limits is untruncated");
eq(graphLimitsNotice({runs: {included: 200, total: 312}}),
  "최근 실행 200건으로 그린 그래프입니다 (전체 312건)",
  "notice names both counts");
eq(graphLimitsNotice({runs: {included: 3, total: 3}}), "",
  "no notice when whole");
eq(graphLimitsNotice(null), "", "no notice when missing");
console.log("PASS " + pass);
''';

L10F_ESC_STUBS = r'''
var gMode = "focus", gFocus = "F", graphSel = "S", gNeedFit = false;
var gFocusReturn = null;
var __calls = [];
var __refocusResult = true;
var __refocusArg = "UNCALLED";
function clearGraphPanel() { __calls.push("clear"); }
function syncHash() { __calls.push("sync"); }
function paintGraph() { __calls.push("paint"); }
function refocusGraphNode(gid) {
  __refocusArg = gid; __calls.push("refocus"); return __refocusResult;
}
function focusGraphCanvas() { __calls.push("canvas"); }
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function ok(c, msg) { eq(!!c, true, msg); }
''';

L10F_CHECKS_E = r'''
// (e) Esc from focus returns to the origin node when displayed.
gMode = "focus"; gFocus = "F"; graphSel = "S"; gNeedFit = false;
gFocusReturn = "X"; __calls = [];
__refocusResult = true; __refocusArg = "UNCALLED";
gBackOverview();
eq(gMode, "overview", "back resets mode");
eq(gFocus, null, "back clears focus");
eq(graphSel, null, "back clears selection");
eq(gFocusReturn, null, "return consumed one-shot");
eq(__refocusArg, "X", "origin refocus attempted");
eq(__calls.indexOf("canvas"), -1, "canvas not used on return");
eq(__calls.join(","), "clear,sync,paint,refocus", "restore order");
// Origin gone from the view: fall back to the canvas.
gFocusReturn = "GONE"; __calls = [];
__refocusResult = false; __refocusArg = "UNCALLED";
gBackOverview();
eq(__refocusArg, "GONE", "gone origin attempted");
ok(__calls.indexOf("canvas") !== -1, "canvas fallback when gone");
// Hash-driven entry records no origin: straight to canvas.
gFocusReturn = null; __calls = []; __refocusArg = "UNCALLED";
gBackOverview();
eq(__refocusArg, "UNCALLED", "no refocus without origin");
ok(__calls.indexOf("canvas") !== -1, "canvas fallback without origin");
console.log("PASS " + pass);
''';


def _make_home(test):
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    use_temp_home(test, str(Path(tmp.name) / "home"))
    return Path(tmp.name)


def _make_profile(name="t1", github="example/t1"):
    repos.save({"name": name, "github": github, "baseBranch": "main",
                "knowledge": [], "checks": [], "e2e": [],
                "release": {}, "announce": {}})


class L10fStaleSelTest(unittest.TestCase):
    # (a) a selection outside the displayed nodes voids; the lanes
    # and hint scope as if nothing were selected.

    def test_stale_selection_voids_with_center_fallback(self):
        src = _script()
        self.assertIn("function validGraphSel(", src,
                      "missing stale-selection helper")
        self.assertIn("function laneScope(", src, "missing lane scope")
        script = (L10F_STUBS + "\n" + _decl(src, "validGraphSel")
                  + "\n" + _decl(src, "laneScope")
                  + "\n" + L10F_CHECKS_A)
        proc = _run_node(script)
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout, f"L10f(a) failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")

    def test_modeful_hash_repro_voids_stale_queue(self):
        src = _script()
        for name in ("parseHash", "clearGraphPanel", "applyGraphHashState",
                     "validGraphSel", "laneScope"):
            self.assertIn("function %s(" % name, src,
                          "missing helper " + name)
        script = (L10F_HASH_STUBS + "\n" + _decl(src, "parseHash")
                  + "\n" + _decl(src, "clearGraphPanel")
                  + "\n" + _decl(src, "applyGraphHashState")
                  + "\n" + _decl(src, "validGraphSel")
                  + "\n" + _decl(src, "laneScope")
                  + "\n" + L10F_CHECKS_AREPRO)
        proc = _run_node(script)
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout,
                         f"L10f(a-repro) failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")

    def test_paint_scopes_lanes_and_hint_by_valid_selection(self):
        paint = _fn_body(_script(), "paintGraph")
        self.assertIn("const effSel = validGraphSel(graphSel, nodes);", paint,
                      "paint must void a stale selection")
        self.assertIn("nodes.some(n => n.id === gFocus), effSel, gFocus,",
                      paint, "lane scope must read the valid selection")
        self.assertIn("(!narrowTree && !effSel)", paint,
                      "hint voice must read the valid selection")


class L10fHashAllTest(unittest.TestCase):
    # (b) a modeless hash leaves mode=all for the overview.

    def test_modeless_hash_leaves_all(self):
        src = _script()
        for name in ("parseHash", "clearGraphPanel", "applyGraphHashState"):
            self.assertIn("function %s(" % name, src,
                          "missing hash helper " + name)
        self.assertIn('gMode === "all"', _fn_body(src, "applyGraphHashState"),
                      "modeless reset must cover mode=all")
        script = (L10F_HASH_STUBS + "\n" + _decl(src, "parseHash")
                  + "\n" + _decl(src, "clearGraphPanel")
                  + "\n" + _decl(src, "applyGraphHashState")
                  + "\n" + L10F_CHECKS_B)
        proc = _run_node(script)
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout, f"L10f(b) failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")


class L10hBuildLimitsTest(unittest.TestCase):
    # (c) /api/graph reports run truncation from the same scan that
    # feeds the nodes, so the metadata can never disagree with the
    # content. No separate cache: freshness across builds.

    def _seed(self, count, repo="t1", issue="7"):
        for i in range(count):
            store.create(repo, issue, "limit job %03d" % i,
                         "limbase%03d" % i, {"worker": "t"})

    def test_truncated_reports_exact_limits(self):
        _make_home(self)
        _make_profile()
        self._seed(205)
        out = graph.build("")
        self.assertIn("nodes", out)
        self.assertIn("edges", out)
        self.assertIn("limits", out)
        self.assertEqual(out["limits"],
                         {"runs": {"included": 200, "total": 205}})

    def test_whole_reports_whole(self):
        _make_home(self)
        _make_profile()
        self._seed(3)
        out = graph.build("")
        self.assertEqual(out["limits"],
                         {"runs": {"included": 3, "total": 3}})

    def test_limits_track_store_growth_across_builds(self):
        _make_home(self)
        _make_profile()
        self._seed(3)
        first = graph.build("")
        self.assertEqual(first["limits"]["runs"]["total"], 3)
        self._seed(202)
        second = graph.build("")
        self.assertEqual(second["limits"],
                         {"runs": {"included": 200, "total": 205}})

    def test_single_scan_still_holds_with_limits(self):
        _make_home(self)
        _make_profile()
        with mock.patch.object(store, "list_runs_with_total",
                               side_effect=lambda limit=20: ([], 0)) as m:
            out = graph.build("")
        self.assertEqual(m.call_count, 1)
        self.assertEqual(out["limits"], {"runs": {"included": 0, "total": 0}})


class L10hWordingTest(unittest.TestCase):
    # (d) truncated wording for the focus subtitle and the one
    # chrome disclosure; untruncated forms byte-identical.

    def test_subtitle_truncation_forms(self):
        src = _script()
        self.assertIn("function focusQuerySub(", src, "missing subtitle")
        script = (L10F_STUBS + "\n" + _decl(src, "focusQuerySub")
                  + "\n" + L10F_CHECKS_DSUB)
        proc = _run_node(script)
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout,
                         f"L10h(d-sub) failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")

    def test_limits_notice_forms(self):
        src = _script()
        self.assertIn("function graphTruncated(", src, "missing detector")
        self.assertIn("function graphLimitsNotice(", src, "missing notice")
        script = (L10F_STUBS + "\n" + _decl(src, "graphTruncated")
                  + "\n" + _decl(src, "graphLimitsNotice")
                  + "\n" + L10F_CHECKS_DNOTICE)
        proc = _run_node(script)
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout,
                         f"L10h(d-notice) failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")

    def test_paint_wires_truncation_flag_and_notice(self):
        src = _script()
        paint = _fn_body(src, "paintGraph")
        self.assertIn("graphTruncated(graphCache && graphCache.limits)",
                      paint, "paint must read the cached limits")
        self.assertIn("fq.capped, graphTrunc)", paint,
                      "subtitle must take the truncation flag")
        self.assertIn("setGraphLimits(graphLimitsNotice(", paint,
                      "paint must set the chrome notice")
        self.assertIn("function setGraphLimits(", src, "missing setter")
        self.assertIn('id="glimits"', _fn_body(src, "renderGraphView"),
                      "missing notice element")


class L10fEscFocusTest(unittest.TestCase):
    # (e) leaving focus returns keyboard focus to the origin node
    # when still displayed, else the canvas. One-shot, no scroll
    # jump (both primitives use preventScroll).

    def test_back_restores_origin_or_canvas(self):
        src = _script()
        self.assertIn("function gBackOverview(", src, "missing back")
        script = (L10F_ESC_STUBS + "\n" + _decl(src, "gBackOverview")
                  + "\n" + L10F_CHECKS_E)
        proc = _run_node(script)
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout, f"L10f(e) failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")

    def test_focus_entry_records_return(self):
        src = _script()
        self.assertIn("let gFocusReturn", src, "missing return slot")
        self.assertIn("gFocusReturn = gid", _fn_body(src, "gFocusNode"),
                      "focus entry must record the origin")
        self.assertIn("gFocusReturn", _fn_body(src, "gBackOverview"),
                      "back must consume the origin")


class L10fGoldensTest(unittest.TestCase):
    # (f) 253/255/257 outputs unchanged, reused by reference.

    def test_wide_goldens_reused(self):
        from tests.test_251_followups import (
            L10CD_WIDE_GOLDEN, _l10cd_wide_outputs)
        from tests.test_255_followups import (
            L10E_WIDE_FOCUS_GOLDEN, _l10e_wide_focus_outputs)
        self.assertTrue(L10CD_WIDE_GOLDEN, "253 golden missing")
        self.assertEqual(_l10cd_wide_outputs(), L10CD_WIDE_GOLDEN)
        self.assertTrue(L10E_WIDE_FOCUS_GOLDEN, "255 golden missing")
        outs, bad = _l10e_wide_focus_outputs()
        self.assertEqual(outs, L10E_WIDE_FOCUS_GOLDEN)
        self.assertEqual(bad, [], "wide fixture stays clean")

    def test_narrow_goldens_reused(self):
        from tests.test_257_followups import (
            L10F_NARROW_FOCUS_GOLDEN, L10F_NARROW_FOCUS_POS,
            L10F_NARROW_TREE_GOLDEN, L10F_NARROW_TREE_POS,
            _l10f_golden_outputs)
        self.assertTrue(L10F_NARROW_TREE_GOLDEN, "257 tree golden missing")
        outs = _l10f_golden_outputs(
            L10F_NARROW_TREE_POS,
            'graphSel = "K1"; narrowTree = true; narrowCol = true;\n'
            'gMode = "overview"; ovNarrow = true;\n'
            'relLanes = incidentLaneMap(TEDGES, TPOS, "K1", nodeR, true);\n'
            "hiddenRels = 0; drawnRels = 0; relHl = new Set();",
            "TEDGES.map(e => __renderEdge(e, TPOS))")
        self.assertEqual(outs, L10F_NARROW_TREE_GOLDEN)
        self.assertTrue(L10F_NARROW_FOCUS_GOLDEN, "257 focus golden missing")
        outs = _l10f_golden_outputs(
            L10F_NARROW_FOCUS_POS,
            'graphSel = null; gFocus = "F"; narrowTree = false;\n'
            'gMode = "focus"; ovNarrow = true; narrowCol = true;\n'
            'relLanes = incidentLaneMap(QEDGES2, QPOS, "F", nodeR, false);\n'
            "hiddenRels = 0; drawnRels = 0; relHl = new Set();",
            "QEDGES2.map(e => __renderEdge(e, QPOS))")
        self.assertEqual(outs, L10F_NARROW_FOCUS_GOLDEN)


if __name__ == "__main__":
    unittest.main()


