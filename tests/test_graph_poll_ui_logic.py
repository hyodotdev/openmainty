"""Focused UI-logic regressions for graph poll/resize, aggregate labels,
and the unified statusbar (dashboard.html).

Pure helpers are extracted from the shipped ``src/openmainty/dashboard.html``
inline script and executed with node, so the test verifies actual logic
(container-generation guard, aggregate category+count labels, branch
member-state counts) rather than mirroring static strings. Statusbar
single-bar invariants are checked structurally on the shipped script.
"""

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src" / "openmainty" / "dashboard.html"

FUNCTIONS = [
    "gfStatusOptions",
    "graphContainerChanged",
    "graphCssW",
    "gDetailIsNew",
    "gDetailSnapshot",
    "gDetailRestore",
    "gDetailFinish",
    "graphContentSig",
    "graphSelAlive",
    "graphPollRefresh",
    "graphFetchDone",
    "graphPagerPrev",
    "graphPagerNext",
    "renderGraphView",
    "graphLoadSecsText",
    "graphLoadDigitEl",
    "startGraphLoadTick",
    "stopGraphLoadTick",
    "setGraphStatus",
    "setGraphMsg",
    "setGraphWarning",
    "graphEmptyKind",
    "graphFiltered",
    "graphSearchHits",
    "clearGraphSearch",
    "clearGraphFilters",
    "runNodeTitle",
    "branchIsActual",
    "branchStateOf",
    "aggBranchSummary",
    "gLabel",
    "relTitle",
    "aggMemberHead",
    "graphRepoOf",
    "buildAggView",
    "expandedGroupLayout",
    "elbowBranchD",
    "groupSpineD",
    "edgeLabelVisible",
    "splitScopeLabel",
    "captionCapOk",
    "gLabelW",
    "graphLabelSide",
]

LINE_CONSTS = ["AGG_TYPES", "PLANNED_BRANCH", "BR_STATE_KO", "TYPE_KO",
               "G_STALE_WARN", "G_POLL_COOL_MS",
               "G_LOAD_BUSY", "G_LOAD_LONG", "G_LOAD_OVER"]
BLOCK_CONSTS = ["AGG_KO", "EDGE_KO", "GST_KO", "GF_STATUS_KO"]
# Single-line function declarations used verbatim: the generic
# balanced-brace extractors cannot see through regex literals that
# contain a quote character (production esc has /[&<>"]/g).
LINE_FNS = ["esc"]

DRIVER = r'''
const fs = require("fs");
const html = fs.readFileSync(process.argv[1], "utf8");
const m = html.match(/<script>([\s\S]*)<\/script>/);
if (!m) { console.log("FAIL no inline script"); process.exit(1); }
const src = m[1];
const wantFn = process.argv[2].split(",");
const wantLine = process.argv[3].split(",");
const wantBlock = process.argv[4].split(",");
const wantLineFn = process.argv[5].split(",");
function extractFnLine(name) {
  const ln = src.split("\n").find((l) => l.startsWith("function " + name + "("));
  if (!ln) throw new Error("missing line fn " + name);
  if (!ln.trimEnd().endsWith("}")) throw new Error("not a single-line fn " + name);
  return ln;
}

function sliceBalanced(s, i) {
  let depth = 0, q = null, line = false, block = false, esc = false;
  for (; i < s.length; i++) {
    const c = s[i], n = s[i + 1];
    if (line) { if (c === "\n") line = false; continue; }
    if (block) { if (c === "*" && n === "/") { block = false; i++; } continue; }
    if (q) {
      if (esc) { esc = false; continue; }
      if (c === "\\") { esc = true; continue; }
      if (c === q) q = null;
      continue;
    }
    if (c === "/" && n === "/") { line = true; i++; continue; }
    if (c === "/" && n === "*") { block = true; i++; continue; }
    if (c === "'" || c === '"' || c === "`") { q = c; continue; }
    if (c === "{") depth++;
    if (c === "}") { depth--; if (depth === 0) return i + 1; }
  }
  throw new Error("unbalanced");
}
function extractFn(name) {
  const re = new RegExp("(?:^|\\n)(?:async\\s+)?function\\s+" + name + "\\s*\\(");
  const mm = re.exec(src);
  if (!mm) throw new Error("missing function " + name);
  const brace = src.indexOf("{", mm.index);
  return src.slice(mm[0].startsWith("\n") ? mm.index + 1 : mm.index, sliceBalanced(src, brace));
}
function extractConstLine(name) {
  const re = new RegExp("(?:^|\\n)const\\s+" + name + "\\s*=([^;\\n]*;?)");
  const mm = re.exec(src);
  if (!mm) throw new Error("missing const " + name);
  return mm[0].trim();
}
function extractConstBlock(name) {
  const re = new RegExp("(?:^|\\n)const\\s+" + name + "\\s*=\\s*\\{");
  const mm = re.exec(src);
  if (!mm) throw new Error("missing const block " + name);
  const brace = src.indexOf("{", mm.index);
  return "const " + name + " = " + src.slice(brace, sliceBalanced(src, brace)) + ";";
}

const stubs = `
let view = {repo: "", kind: "command"};
function esc(s) { return String(s == null ? "" : s); }
// poll/resize harness: poll state lives here so the real graphPollRefresh
// closes over it; paintGraph is a recording stub (the real one needs layout).
// The request-ownership fix adds generation/sequence/status state, a mock DOM
// with per-generation identity, and a call-logged jget.
var gNeedFit = false, gLayoutGen = 0, gVBCanvasW = 0, graphCache = null,
  graphSel = null, svgW = 683, paintCalls = 0, jgetResolve = null;
var gGraphGen = 0, gPollSeq = 0, gStatusMsg = null, gStatusEl = null,
  graphRepo = "__none__";
var gGraphFetch = null, gPollCoolUntil = 0;
var gMode = "overview", gFocus = null, gQuery = "", gDepOpen = false,
  gClientType = "", gClientStatus = "", gDetailKey = null;
var O = {repos: []};
function syncHash() {}
function gBackOverview() {}
function bindPan() {}
function graphZoom() {}
function applyZoom() {}
function loadAll() { return Promise.resolve(); }
function jpost() { return Promise.resolve({}); }
function toast() {}
function focusViewHeading() { return false; }
function graphColor() { return "#000"; }
function mkEl(id) {
  return {
    id: id, innerHTML: "", textContent: "", value: "", onclick: null,
    dataset: {}, style: {}, _qs: {}, _attrs: {},
    classList: { toggle: function () {}, add: function () {}, remove: function () {} },
    setAttribute: function (k, v) { this._attrs[k] = v; },
    getAttribute: function (k) { return (k in this._attrs) ? this._attrs[k] : null; },
    removeAttribute: function (k) { delete this._attrs[k]; },
    querySelector: function (sel) {
      if (!this._qs[sel]) this._qs[sel] = mkEl(this.id + sel);
      return this._qs[sel];
    },
    querySelectorAll: function () { return []; },
    appendChild: function (k) { return k; },
    getBoundingClientRect: function () { return {width: svgW}; }
  };
}
var document = {createElement: function () { return mkEl("gloadspan"); }};
var __els = {};
var gsvgEl = null;
function freshGsvg() { gsvgEl = mkEl("gsvg"); }
freshGsvg();
var viewEl = mkEl("view");
Object.defineProperty(viewEl, "innerHTML", {
  configurable: true,
  get: function () { return this._html || ""; },
  set: function (v) {
    this._html = v;
    freshGsvg();
    for (const k in __els) delete __els[k];
  }
});
function $(id) {
  if (id === "gsvg") return gsvgEl;
  if (id === "view") return viewEl;
  if (!__els[id]) __els[id] = mkEl(id);
  return __els[id];
}
function fitGraphChrome() {}
function paintGraph() { paintCalls++; if (gNeedFit) gNeedFit = false; }
// DOM-only panel/focus helpers: record calls; the real ones need layout.
var refocusCalls = 0, refocusLast = null, clearCalls = 0;
function refocusGraphNode(gid) { refocusCalls++; refocusLast = gid; return true; }
function clearGraphPanel() { clearCalls++; }
var jgetLog = [];
function jget(u) {
  var rec = {u: u, res: null, rej: null};
  jgetLog.push(rec);
  return new Promise(function (resolve, reject) {
    rec.res = resolve; rec.rej = reject; jgetResolve = resolve;
  });
}
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function ok(c, msg) { eq(!!c, true, msg); }
`;

const tests = `
;(async () => {
// 1) container-generation guard: user zoom (gVB.w) must never be compared;
//    only actual canvas widths, before vs after. 8px tolerance for subpixels.
eq(graphContainerChanged(316, 683), true, "desktop683->mobile316 forces refit");
eq(graphContainerChanged(683, 316), true, "mobile316->desktop683 forces refit");
eq(graphContainerChanged(316, 316), false, "steady 390 poll keeps zoom");
eq(graphContainerChanged(320, 316), false, "4px subpixel wobble keeps zoom");
eq(graphContainerChanged(0, 683), false, "unmeasurable now is unknown, not a refit");
eq(graphContainerChanged(316, 0), false, "unmeasurable then is unknown, not a refit");

// 2) aggregate group nodes use category+count, never the explanatory detail copy
const runAgg = {id: "agg:r:run", type: "run", agg: true, repo: "r", count: 8,
  label: "실행 8건", status: "ok",
  detail: "실제 8건을 묶어 보여줍니다 · 주의 0건"};
eq(gLabel(runAgg), "실행 8건", "run aggregate shows category+count, not detail copy");
eq(relTitle(runAgg), "실행 8건", "related aggregate shows category+count");
// real run entities still use their actual work title
const realRun = {id: "run:abc123", type: "run", label: "run:abc123 #774",
  title: "슬라이더 고장", detail: "슬라이더 고장 · lane=1행"};
eq(gLabel(realRun), "슬라이더 고장 #774", "real run uses its title");

// 3) branch aggregate with one created + 43 planned member: no all-planned claim
const members = [{id: "branch:r:dep-x", type: "branch", repo: "r",
    label: "dep-x", planned: false, exists: true}];
for (let i = 0; i < 43; i++) members.push({id: "branch:r:q" + i, type: "branch",
  repo: "r", label: "q" + i, planned: true});
const nodes = [{id: "repo:r", type: "repo", label: "r", repo: "r"}].concat(members);
const agg = buildAggView(nodes, []);
const brAgg = agg.gnodes.find((n) => n.id === "agg:r:branch");
ok(brAgg, "branch aggregate exists");
eq(brAgg.label, "브랜치 44건(생성 1·예정 43)", "mixed branch aggregate counts members");
eq(gLabel(brAgg), "브랜치 44건(생성 1·예정 43)", "no (예정) suffix on aggregate");
ok(brAgg.detail.includes("생성 1") && brAgg.detail.includes("예정 43"),
  "inspector detail carries full member counts");
// all-planned group: bare category+count, no false created claim either way
const plannedOnly = [{id: "branch:r:p1", type: "branch", repo: "r", label: "p1", planned: true},
  {id: "branch:r:p2", type: "branch", repo: "r", label: "p2", planned: true}];
const agg2 = buildAggView([{id: "repo:r", type: "repo", label: "r", repo: "r"}].concat(plannedOnly), []);
const brAgg2 = agg2.gnodes.find((n) => n.id === "agg:r:branch");
eq(gLabel(brAgg2), "브랜치 2건", "uniform planned group has no suffix claim");
ok(brAgg2.detail.includes("예정 2"), "uniform detail still reports counts");
// edge-fallback member (no core7 fields) with a works-on edge counts as created
const edgeNodes = [{id: "repo:r", type: "repo", label: "r", repo: "r"},
  {id: "branch:r:real", type: "branch", repo: "r", label: "real"},
  {id: "run:z", type: "run", repo: "r", label: "run:z"}];
const edgeEdges = [{from: "run:z", to: "branch:r:real", kind: "works-on"}];
const c = aggBranchSummary([edgeNodes[1]], edgeEdges);
eq(c.actual, 1, "works-on member counts as created without core7 fields");

// 4) async poll + resize between request and response (real graphPollRefresh)
view.kind = "graph";
graphCache = {nodes: [{id: "a"}], edges: []};
// steady 40s poll at same width: no refit flag, no repaint on same data
svgW = 316; gVBCanvasW = 316; gNeedFit = false; paintCalls = 0;
let p1 = graphPollRefresh();
await new Promise((r) => setTimeout(r, 0));
jgetResolve({nodes: [{id: "a"}], edges: []});
await p1;
eq(gNeedFit, false, "steady poll at same width keeps user zoom");
eq(paintCalls, 0, "steady poll with same data does not repaint");
// desktop 683 -> mobile 316 DURING the await: stale response must refit
svgW = 683; gVBCanvasW = 683; gNeedFit = false; paintCalls = 0;
let p2 = graphPollRefresh();
svgW = 316;
await new Promise((r) => setTimeout(r, 0));
jgetResolve({nodes: [{id: "a"}], edges: []});
await p2;
eq(paintCalls, 1, "same-data poll after resize still repaints for readable fit");
// resize back 316 -> 683 during the await: also refits, no click needed
svgW = 316; gVBCanvasW = 316; gNeedFit = false; paintCalls = 0;
let p3 = graphPollRefresh();
svgW = 683;
await new Promise((r) => setTimeout(r, 0));
jgetResolve({nodes: [{id: "a"}], edges: []});
await p3;
eq(paintCalls, 1, "resize back to desktop refits without manual fit click");
// 4b) same-count semantic change with a valid aggregate selection repaints
//     the new facts but keeps the selection and its inspector (no clear).
//     Render decoration (_-keys stamped by paintGraph) is not content:
//     decorated cache vs the same raw API facts must stay quiet.
svgW = 316; gVBCanvasW = 316;
const depNodes = [{id: "repo:r", type: "repo", label: "r", repo: "r"},
  {id: "dep:r:a", type: "dependency", repo: "r", label: "a", status: "ok"},
  {id: "dep:r:b", type: "dependency", repo: "r", label: "b", status: "ok"}];
const depEdges = [{from: "repo:r", to: "dep:r:a", kind: "tracks", provenance: "p1"}];
graphCache = {nodes: depNodes, edges: depEdges};
graphSel = "agg:r:dependency";
gNeedFit = false; paintCalls = 0; clearCalls = 0; refocusCalls = 0;
let p4 = graphPollRefresh();
await new Promise((r) => setTimeout(r, 0));
jgetResolve({nodes: [{id: "repo:r", type: "repo", label: "r", repo: "r"},
  {id: "dep:r:a", type: "dependency", repo: "r", label: "a", status: "attention"},
  {id: "dep:r:b", type: "dependency", repo: "r", label: "b", status: "ok"}],
  edges: [{from: "repo:r", to: "dep:r:a", kind: "tracks", provenance: "p2"}]});
await p4;
eq(paintCalls, 1, "same-count status/edge-provenance change repaints");
eq(graphSel, "agg:r:dependency", "valid aggregate selection survives a data poll");
eq(clearCalls, 0, "valid selection never clears the inspector");
// decorated cache (paintGraph stamps _-keys) vs identical raw facts: quiet
graphCache = {nodes: [{id: "repo:r", type: "repo", label: "r", repo: "r"},
  {id: "dep:r:a", type: "dependency", repo: "r", label: "a", status: "attention"},
  {id: "dep:r:b", type: "dependency", repo: "r", label: "b", status: "ok"}],
  edges: [{from: "repo:r", to: "dep:r:a", kind: "tracks", provenance: "p2"}]};
graphCache.nodes.forEach(function (n, i) { n._x = 10 * i; n._fullLabel = n.label; });
gNeedFit = false; paintCalls = 0; clearCalls = 0;
let p5 = graphPollRefresh();
await new Promise((r) => setTimeout(r, 0));
jgetResolve({nodes: [{id: "repo:r", type: "repo", label: "r", repo: "r"},
  {id: "dep:r:a", type: "dependency", repo: "r", label: "a", status: "attention"},
  {id: "dep:r:b", type: "dependency", repo: "r", label: "b", status: "ok"}],
  edges: [{from: "repo:r", to: "dep:r:a", kind: "tracks", provenance: "p2"}]});
await p5;
eq(paintCalls, 0, "render decoration alone never repaints or drops focus");
eq(graphSel, "agg:r:dependency", "quiet poll keeps the aggregate selection");
graphSel = null;

// 5) inspector member heads: full wrapped titles, number/kind secondary,
//    no premature truncation in the group list
const prm = {id: "issue:r#774", type: "issue", repo: "r", label: "r#774",
  title: "fix: safeDouble overflow handling in chart pipeline",
  issue: "774", object_kind: "pr"};
const pmh = aggMemberHead(prm);
eq(pmh.primary, "fix: safeDouble overflow handling in chart pipeline",
  "group member keeps the full actual title");
eq(pmh.secondary, "#774 · PR", "group member number/kind secondary");
const longTitle = "a".repeat(120);
const lm = aggMemberHead({id: "issue:r#1", type: "issue", title: longTitle, issue: "1"});
eq(lm.primary.length, 120, "long titles are never cut to label width");
// relation names stay human-readable (raw kind remains in technical details)
eq(EDGE_KO.mentions, "언급", "mentions renders as 언급");
eq(EDGE_KO.about, "관련", "about renders as 관련");

// 6) inspector scroll: a new selection (first open included) resets the
//    detail panel to its title top; a same-item poll re-render restores the
//    user's scroll, open evidence <details>, and focus — never 0.
eq(gDetailIsNew(null, "issue:r#774"), true, "first open counts as new selection");
eq(gDetailIsNew("issue:r#774", "issue:r#774"), false, "same-item poll is not new");
eq(gDetailIsNew("agg:r:issue", "issue:r#774"), true, "bundle -> member resets to title");
eq(gDetailIsNew("agg:r:issue", "agg:r:issue"), false, "same bundle poll preserves");
function fakeBox() {
  const details = [{open: true}, {open: false}, {open: true}];
  const focusBtn = {id: "gd-focus", _focused: null,
    focus: function (o) { this._focused = o || {}; }};
  const aggmBtn = {_focused: null,
    focus: function (o) { this._focused = o || {}; }};
  return {scrollTop: 137, _details: details, _btn: focusBtn, _aggm: aggmBtn,
    querySelectorAll: function () { return this._details; },
    contains: function () { return false; },
    querySelector: function (sel) {
      if (sel === "#gd-focus") return this._btn;
      if (sel === '[data-aggm="issue:r#774"]') return this._aggm;
      return null;
    }};
}
var fakeActive = {id: "gd-focus", getAttribute: function () { return null; }};
const fb = fakeBox();
fb.contains = function (el) { return el === fakeActive; };
const snap = gDetailSnapshot(fb, fakeActive);
eq(snap.scroll, 137, "snapshot keeps the user's scroll position");
eq(JSON.stringify(snap.open), JSON.stringify([true, false, true]), "snapshot keeps open evidence");
eq(snap.focusKey, "gd-focus", "snapshot keeps the panel focus");
// clobber the panel the way a re-render would, then restore
fb.scrollTop = 999;
fb._details.forEach(function (d) { d.open = false; });
gDetailRestore(fb, snap);
eq(fb.scrollTop, 137, "same-item poll restores scroll, never 0");
eq(JSON.stringify(fb._details.map(function (d) { return d.open; })),
  JSON.stringify([true, false, true]), "same-item poll restores open evidence");
eq(JSON.stringify(fb._btn._focused), JSON.stringify({preventScroll: true}),
  "same-item poll restores focus without moving scroll");
// data-aggm buttons (bundle member titles) restore by member id
var fakeActive2 = {id: "", getAttribute: function (k) { return k === "data-aggm" ? "issue:r#774" : null; }};
const fb2 = fakeBox();
fb2.contains = function (el) { return el === fakeActive2; };
const snap2 = gDetailSnapshot(fb2, fakeActive2);
eq(snap2.focusKey, "issue:r#774", "snapshot keeps bundle member focus");
gDetailRestore(fb2, snap2);
eq(JSON.stringify(fb2._aggm._focused), JSON.stringify({preventScroll: true}),
  "bundle member focus restores without moving scroll");
// finish: new selection resets to the title top and returns the next key
const nb = fakeBox();
eq(gDetailFinish(nb, null, "issue:r#774", null), "issue:r#774", "finish returns the next key");
eq(nb.scrollTop, 0, "new selection resets to the title top");
const nb2 = fakeBox();
nb2.scrollTop = 200;
eq(gDetailFinish(nb2, "agg:r:issue", "issue:r#774", null), "issue:r#774", "bundle -> member returns member key");
eq(nb2.scrollTop, 0, "bundle -> member resets scrolled bundle position");
// finish: same-item poll preserves instead of resetting
const sb = fakeBox();
sb.contains = function (el) { return el === fakeActive; };
const ss = gDetailSnapshot(sb, fakeActive);
sb.scrollTop = 0;
sb._details.forEach(function (d) { d.open = false; });
eq(gDetailFinish(sb, "issue:r#774", "issue:r#774", ss), "issue:r#774", "same key returns itself");
eq(sb.scrollTop, 137, "same-item poll finish preserves scroll");
eq(JSON.stringify(sb._details.map(function (d) { return d.open; })),
  JSON.stringify([true, false, true]), "same-item poll finish preserves evidence");

// 7) expanded group: single indented column + spine branches, never a fan.
//    57 members keep count and order, share one x, and space rows so labels
//    cannot overlap; every branch is horizontal and ends at its circle's
//    left edge while labels start right of the circle.
const ids57 = [];
for (let i = 0; i < 56; i++) ids57.push("dep:m" + i);
const lay = expandedGroupLayout(ids57, 92, 100, 62);
eq(lay.length, 56, "expanded group keeps all 56 members");
eq(lay[0].x, 92, "members share one indented column");
eq(lay[0].y, 100, "column starts below the repo node");
ok(lay.every(function (p, i) { return p.id === ids57[i]; }), "member order preserved");
ok(lay.every(function (p) { return p.x === 92; }), "no second column exists");
ok(lay.every(function (p, i) { return i === 0 || p.y - lay[i - 1].y >= 62; }),
  "row spacing keeps labels apart");
const R = 9, SPINE = 60;
ok(lay.every(function (p) { return SPINE < p.x - R - 1; }), "spine stays left of every circle");
lay.forEach(function (p) {
  const d = elbowBranchD(SPINE, p.y, p.x, R);
  eq(d, "M 60 " + p.y + " H " + (p.x - R - 1), "branch is horizontal at member height");
});
eq(groupSpineD(60, 46, 100 + 55 * 62), "M 60 46 V " + (100 + 55 * 62), "spine is one vertical run");
// branch endpoints never reach label starts (circle right edge + 16px gap)
ok(lay.every(function (p) { return (p.x - R - 1) < (p.x + 16); }), "branches stop before labels begin");
// relation captions only on the selected edge, never a 56-caption stack
const ee = {from: "repo:r", to: "dep:m0", kind: "tracks"};
eq(edgeLabelVisible("group", null, ee), false, "expanded group shows no captions unselected");
eq(edgeLabelVisible("focus", null, ee), false, "focus shows no captions unselected");
eq(edgeLabelVisible("overview", null, ee), false, "overview shows no captions");
eq(edgeLabelVisible("group", "dep:m0", ee), true, "selected edge keeps its caption");
eq(edgeLabelVisible("group", "repo:r", ee), true, "selection from either end keeps caption");

// 8) scoped labels split scope/name so the identifying tail survives;
//    high-degree selection suppresses the caption pile entirely.
eq(JSON.stringify(splitScopeLabel("@typescript-eslint/eslint-plugin")),
  JSON.stringify(["@typescript-eslint/", "eslint-plugin"]), "scope/name split at last slash");
eq(JSON.stringify(splitScopeLabel("@commitlint/config-conventional")),
  JSON.stringify(["@commitlint/", "config-conventional"]), "tail kept whole");
eq(JSON.stringify(splitScopeLabel("del-cli")), JSON.stringify(["del-cli"]), "unscoped stays one line");
eq(JSON.stringify(splitScopeLabel("")), JSON.stringify([""]), "empty stays one line");
eq(captionCapOk(6), true, "six selected edges still label");
eq(captionCapOk(7), false, "seven selected edges suppress captions");
eq(captionCapOk(56), false, "selected repo with 56 edges shows no caption pile");
eq(captionCapOk(0), true, "no selection is not a pile");

// 9) 390 single-column tree: long scoped members label RIGHT with the real
//    remaining width, never end-anchored crumbs in the 70px left gutter.
//    panelW=316 (390 page), member x=92, full '@react-native-community/slider'.
const full390 = "@react-native-community/slider";
const side390 = graphLabelSide(full390, 92, 316, true);
eq(side390.anchor, "start", "single-column member labels right, not end");
eq(side390.avail, 200, "avail is the real right-side remainder, not the 68px gutter");
ok(side390.avail > 100, "old gutter branch (68px) must not come back");
const parts390 = splitScopeLabel(full390);
eq(parts390.join(""), full390, "wrapped lines keep the full identity, no hidden chars");
ok(gLabelW(parts390[1]) <= side390.avail, "identifying tail 'slider' fits whole");
ok(gLabelW(parts390[0]) - side390.avail <= 14, "scope line loses at most an ellipsis");
// desktop grid keeps its old behavior: short labels right, far-right long ones left
eq(graphLabelSide("del-cli", 300, 1197, false).anchor, "start", "desktop grid short label stays right");
eq(graphLabelSide(full390, 1000, 1197, false).anchor, "end", "desktop grid far-right still flips left");

// 10) request ownership: late/stale responses cannot touch the current view.
//     Executes the real renderGraphView/graphPollRefresh with an explicit
//     controllable mock DOM (per-generation identity) and a call-logged
//     mock network. Qualified logic only, not Chrome/pixels.
function resetGraphHarness() {
  view.repo = ""; view.kind = "graph";
  graphCache = null; graphRepo = "__none__"; graphSel = null;
  gGraphGen = 0; gPollSeq = 0; gStatusMsg = null; gStatusEl = null;
  gGraphFetch = null; gPollCoolUntil = 0;
  gNeedFit = false; paintCalls = 0; refocusCalls = 0; clearCalls = 0;
  gClientType = ""; gClientStatus = ""; gQuery = "";
  jgetLog.length = 0;
  for (const k in __els) delete __els[k];
  viewEl._html = "";
  freshGsvg();
}
function tick() { return new Promise((r) => setTimeout(r, 0)); }
// 10a) A -> B: late A success never mutates the cache, repo, or paint.
resetGraphHarness();
view.repo = "A";
let rA = renderGraphView();
eq(jgetLog.length, 1, "A issues its owned fetch");
view.repo = "B";
let rB = renderGraphView();
eq(jgetLog.length, 2, "B issues its own fetch");
jgetLog[0].res({nodes: [{id: "aA"}], edges: []});
await rA;
eq(graphCache, null, "late A success never writes the cache");
eq(graphRepo === "A", false, "late A success never binds the repo");
eq(paintCalls, 0, "late A success never paints");
jgetLog[1].res({nodes: [{id: "bB"}], edges: []});
await rB;
eq(graphRepo, "B", "repo is recorded from the captured request only");
eq(graphCache.nodes[0].id, "bB", "current B data is applied");
eq(paintCalls, 1, "current B paints exactly once");
// 10b) A -> B: late A failure never shows in B; B still recovers.
resetGraphHarness();
view.repo = "A";
let fA = renderGraphView();
view.repo = "B";
let fB = renderGraphView();
jgetLog[0].rej(new Error("gone"));
await fA;
ok($("gdetail").innerHTML.indexOf("그래프 조회 실패") === -1,
  "stale A failure never surfaces in the current repo");
jgetLog[1].res({nodes: [{id: "bB"}], edges: []});
await fB;
eq(graphRepo, "B", "B recovers after a dropped A failure");
// 10c) A -> B -> A: neither old response wins.
resetGraphHarness();
view.repo = "A"; let g1 = renderGraphView();
view.repo = "B"; let g2 = renderGraphView();
view.repo = "A"; let g3 = renderGraphView();
eq(jgetLog.length, 3, "each navigation issues an owned fetch");
jgetLog[0].res({nodes: [{id: "oldA"}], edges: []}); await g1;
eq(graphCache, null, "superseded first A leaves the cache alone");
jgetLog[1].res({nodes: [{id: "bB"}], edges: []}); await g2;
eq(graphCache, null, "superseded B leaves the cache alone");
jgetLog[2].res({nodes: [{id: "newA"}], edges: []}); await g3;
eq(graphRepo, "A", "final A binds the repo");
eq(graphCache.nodes[0].id, "newA", "final A data wins");
// 10d) same-repo overlapping polls: the second never starts a fetch.
//     L1(243) supersedes newest-wins-overlap: a 15s graph fetch must
//     never pile a second 15s request behind it. The poll-sequence
//     guard stays in production as defense, but overlap is no longer
//     reachable through the poll path.
resetGraphHarness();
view.repo = "r";
graphCache = {nodes: [{id: "a"}], edges: []}; graphRepo = "r";
let q1 = graphPollRefresh();
let q2 = graphPollRefresh();
eq(jgetLog.length, 1, "overlapping poll never starts a second fetch");
await q2;
jgetLog[0].res({nodes: [{id: "b"}], edges: []}); await q1;
eq(graphCache.nodes[0].id, "b", "the single poll result is applied");
eq(paintCalls, 1, "the single poll paints exactly once");
// 10d2) unchanged poll results cool the tick; explicit retry bypasses.
resetGraphHarness();
view.repo = "r";
graphCache = {nodes: [{id: "a"}], edges: []}; graphRepo = "r";
let qc = graphPollRefresh();
eq(jgetLog.length, 1, "cooling poll issues its fetch");
jgetLog[0].res(JSON.parse(JSON.stringify(graphCache))); await qc;
eq(paintCalls, 0, "identical poll repaints nothing");
ok(gPollCoolUntil > Date.now(), "identical poll cools the tick");
const cooledCalls = jgetLog.length;
await graphPollRefresh();
eq(jgetLog.length, cooledCalls, "cooled tick issues no fetch");
let qf = graphPollRefresh(true);
eq(jgetLog.length, cooledCalls + 1, "forced retry bypasses the cool");
jgetLog[jgetLog.length - 1].res({nodes: [{id: "z"}], edges: []});
await qf;
eq(graphCache.nodes[0].id, "z", "forced retry applies changed data");
eq(gPollCoolUntil, 0, "changed data resets the cool");
let qn = graphPollRefresh();
eq(jgetLog.length, cooledCalls + 2, "reset cool lets the tick fetch again");
jgetLog[jgetLog.length - 1].res({nodes: [{id: "z"}], edges: []});
await qn;
// 10d3) same-repo double render shares one load (palette/boot double
//     render for one navigation): the second render joins the first
//     response instead of firing its own 20 s request.
resetGraphHarness();
view.repo = "r";
graphCache = null; graphRepo = "__none__";
let s1 = renderGraphView();
let s2 = renderGraphView();
eq(jgetLog.length, 1, "double render issues a single fetch");
jgetLog[0].res({nodes: [{id: "s"}], edges: []});
await s1; await s2;
eq(graphRepo, "r", "shared load binds the repo");
eq(graphCache.nodes[0].id, "s", "shared load applies its data");
eq(paintCalls, 1, "shared load paints exactly once");
// 10d4) a poll during an initial load neither fetches nor disturbs it;
//     the cool window is armed from the moment the load starts.
resetGraphHarness();
view.repo = "r";
graphCache = null; graphRepo = "__none__";
let w1 = renderGraphView();
ok(gPollCoolUntil > Date.now(), "cool is armed when the load starts");
const sharedCalls = jgetLog.length;
await graphPollRefresh();
eq(jgetLog.length, sharedCalls, "poll during a load issues no fetch");
jgetLog[sharedCalls - 1].res({nodes: [{id: "w"}], edges: []});
await w1;
eq(graphRepo, "r", "initial load binds the repo");
eq(paintCalls, 1, "initial load paints exactly once");
// 10e) abandoned DOM: a rebuilt panel drops the pending response.
resetGraphHarness();
view.repo = "r";
graphCache = {nodes: [{id: "a"}], edges: []}; graphRepo = "r";
let qd = graphPollRefresh();
viewEl.innerHTML = "<div>rebuilt</div>";
jgetLog[0].res({nodes: [{id: "z"}], edges: []}); await qd;
eq(graphCache.nodes[0].id, "a", "response for an abandoned DOM is dropped");
eq(paintCalls, 0, "abandoned DOM never paints");
// 10f) empty-state classifier: real cache counts, filters before search.
resetGraphHarness();
graphCache = null; gQuery = "";
eq(graphEmptyKind(), "empty", "no cache is truly empty");
graphCache = {nodes: [{id: "a", type: "issue", label: "a"}], edges: []};
gClientType = ""; gClientStatus = ""; gQuery = "";
eq(graphEmptyKind(), "shown", "unfiltered cache is shown");
gClientType = "run";
eq(graphEmptyKind(), "filtered-zero", "type filter is checked before the query");
gQuery = "zzz";
eq(graphEmptyKind(), "filtered-zero", "filter-zero is not blamed on search");
gClientType = "";
eq(graphEmptyKind(), "search-zero", "real post-search zero after passing filters");
gQuery = "a";
eq(graphEmptyKind(), "shown", "a matching query is shown, never search-zero");
graphCache = null; gQuery = "zzz";
eq(graphEmptyKind(), "empty", "empty cache wins over a query");
// 10g) search helper and clear actions run the real production code.
resetGraphHarness();
eq(graphSearchHits([{id: "x", label: "Slider broken"}], "slider").length, 1,
  "search helper matches label text");
eq(graphSearchHits([{id: "x", label: "a"}], "zzz").length, 0,
  "search helper reports zero");
eq(graphSearchHits([{id: "x"}], ""), null, "empty query is not a search");
gQuery = "zzz";
$("g-q").value = "zzz";
clearGraphSearch();
eq(gQuery, "", "clear-search resets the query state");
eq($("g-q").value, "", "clear-search resets the box");
eq(paintCalls, 1, "clear-search requests a repaint");
gClientType = "run"; gClientStatus = "done";
$("gf-type").value = "run"; $("gf-status").value = "done";
clearGraphFilters();
eq(gClientType, "", "clear-filters resets the type state");
eq($("gf-status").value, "", "clear-filters resets the box");
eq(paintCalls, 2, "clear-filters requests a repaint");
// 10h) poll failure preserves everything and warns; recovery clears
//     the warning without repainting identical content.
resetGraphHarness();
view.repo = "r";
graphCache = {nodes: [{id: "a"}], edges: []}; graphRepo = "r";
graphSel = "a"; paintCalls = 0;
let pw = graphPollRefresh();
await tick();
jgetLog[jgetLog.length - 1].rej(new Error("down"));
await pw;
eq(gStatusMsg, G_STALE_WARN, "poll failure keeps the cache and warns");
eq(graphCache.nodes[0].id, "a", "failed poll preserves the canvas cache");
eq(graphSel, "a", "failed poll preserves the selection");
var warnBtn = $("gstatus").querySelector("#g-pretry");
ok(warnBtn && warnBtn.onclick, "warning offers a scoped GET-only retry");
var callsBefore = jgetLog.length;
warnBtn.onclick();
eq(jgetLog.length, callsBefore + 1, "retry issues one scoped GET");
await tick(); await tick();
jgetLog[jgetLog.length - 1].res({nodes: [{id: "a"}], edges: []});
await tick(); await tick(); await tick();
eq(gStatusMsg, "", "identical recovery clears the warning");
eq(paintCalls, 0, "identical recovery repaints nothing");
// 10i) status dedup is per-DOM: a rebuilt panel shows loading again.
resetGraphHarness();
var statusWrites = 0;
var cel = mkEl("gstatus");
Object.defineProperty(cel, "textContent", {
  configurable: true,
  get: function () { return this._t || ""; },
  set: function (v) { this._t = v; statusWrites++; }
});
__els["gstatus"] = cel;
gStatusMsg = null; gStatusEl = null;
setGraphStatus("불러오는 중…");
setGraphStatus("불러오는 중…");
eq(statusWrites, 1, "identical status is not re-announced on the same DOM");
viewEl.innerHTML = "<div>rebuilt</div>";
setGraphStatus("불러오는 중…");
eq(statusWrites, 1, "a new DOM is never written through the old element");
eq($("gstatus").textContent, "불러오는 중…", "the new DOM actually shows loading again");
// 10j) the real escaper neutralizes markup in echoed queries.
ok(esc('<img src=x onerror=alert(1)>').indexOf("<") === -1,
  "actual esc neutralizes markup");
ok(esc('"q"').indexOf('"') === -1, "actual esc neutralizes quotes");
console.log("PASS " + pass);
})();
`;

const pieces = [stubs];
for (const n of wantLine) pieces.push(extractConstLine(n));
for (const n of wantBlock) pieces.push(extractConstBlock(n));
for (const n of wantFn) pieces.push(extractFn(n));
for (const n of wantLineFn) pieces.push(extractFnLine(n));
pieces.push(tests);
try {
  eval(pieces.join("\n"));
} catch (e) { console.log("FAIL eval :: " + (e && e.message)); process.exit(1); }
'''


def _run_node(extra_args):
    return subprocess.run(
        ["node", "-e", DRIVER, str(HTML), ",".join(FUNCTIONS),
         ",".join(LINE_CONSTS), ",".join(BLOCK_CONSTS),
         ",".join(LINE_FNS)] + extra_args,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _script():
    text = HTML.read_text()
    blocks = re.findall(r"<script>(.*?)</script>", text, re.S)
    assert len(blocks) == 1, "expected one inline dashboard script"
    return blocks[0]


def _fn_body(src, name):
    """Balanced-brace body of a top-level (async) function, strings/comments skipped."""
    m = re.search(r"(?:^|\n)(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", src)
    assert m, f"missing function {name}"
    i = src.index("{", m.start())
    depth, q, line, block, esc = 0, None, False, False, False
    for j in range(i, len(src)):
        c, n = src[j], src[j + 1] if j + 1 < len(src) else ""
        if line:
            if c == "\n":
                line = False
            continue
        if block:
            if c == "*" and n == "/":
                block, j = False, j + 1
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


class DashboardScriptSyntaxTest(unittest.TestCase):
    def test_dashboard_script_syntax(self):
        proc = subprocess.run(
            ["node", "--check", "/dev/stdin"],
            input=_script(),
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, f"dashboard JS syntax error: {proc.stderr}")


class GraphPollAggregateLogicTest(unittest.TestCase):
    def test_graph_poll_aggregate_logic(self):
        proc = _run_node([])
        self.assertEqual(
            proc.returncode, 0, f"node driver failed: {proc.stdout}\n{proc.stderr}"
        )
        self.assertNotIn("FAIL", proc.stdout, f"logic failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, f"no passing assertions: {proc.stdout}")


class InspectorSelectionScrollTest(unittest.TestCase):
    def test_detail_scroll_wired_to_selection_change(self):
        src = _script()
        # paintGDetail distinguishes a new selection from a same-item re-render
        self.assertIn("gDetailIsNew(detailPrev, gid)", src)
        # all four detail render paths settle scroll: empty bundle, member
        # bundle, missing node, and the main found-node inspector
        self.assertEqual(
            src.count("gDetailKey = gDetailFinish(box, detailPrev, gid, detailSnap)"),
            4,
            "bundle-empty/bundle/missing/inspector detail paths must all settle scroll",
        )
        # clearing the selection makes the next open a first open (DOM untouched)
        self.assertIn("if (!graphSel) gDetailKey = null;", src)

    def test_reset_happens_only_on_new_selection(self):
        src = _script()
        # inspector: the only place that zeroes the panel is the
        # new-selection branch (same-item polls restore, never reset)
        finish = _fn_body(src, "gDetailFinish")
        self.assertEqual(
            finish.count("scrollTop = 0"), 1,
            "inspector zeroes the panel only on a new selection",
        )
        self.assertIn("gDetailRestore", finish)
        # navigation: the central route reset exists and is gated on an
        # actual route change — it is not a per-poll unconditional reset
        render = _fn_body(src, "render")
        self.assertIn("lastRenderResetScroll = routeScrollReset()", render)
        self.assertIn("scrollTop = 0", render)
        self.assertIn("if (lastRenderResetScroll && $(\"view\"))", render)

    def test_poll_path_never_touches_detail_scroll(self):
        src = _script()
        poll = _fn_body(src, "graphPollRefresh")
        self.assertNotIn("scrollTop", poll)
        self.assertNotIn("gDetail", poll)
        self.assertNotIn("gdetail", poll)


class StatusbarSingleBarTest(unittest.TestCase):
    def test_legacy_floats_never_unhidden(self):
        src = _script()
        self.assertNotIn('jf.classList.remove("hidden")', src)
        self.assertNotIn('rf.classList.remove("hidden")', src)
        self.assertNotIn("jf.innerHTML", src)
        self.assertNotIn("rf.innerHTML", src)

    def test_statusbar_targets_exact_active_work(self):
        src = _script()
        self.assertIn("selRun = liveRunId", src,
                      "live 보기 must open the exact active run")
        self.assertIn("selRun = quotaRun.id", src,
                      "quota 보기 must open the exact waiting run")

    def test_statusbar_inflow_never_overlays(self):
        text = HTML.read_text()
        # Structural: the bar lives in the main column before #view, so it
        # reserves layout space instead of floating over content.
        main = text[text.index('<main class="main">'):text.index("</main>")]
        self.assertLess(main.index('id="statusbar"'), main.index('id="view"'),
                        "statusbar must sit in the main flow before #view")
        self.assertIn('role="status"', main)
        # Non-floating layout contract for the strip itself.
        m = re.search(r"#statusbar\s*\{([^}]*)\}", text)
        self.assertIsNotNone(m, "missing #statusbar rule")
        self.assertIn("position: static", m.group(1))
        self.assertNotIn("fixed", m.group(1))
        # Mobile one-line truncation preserved for the strip text.
        self.assertIn("#statusbar .sb-text", text)
        self.assertIn("text-overflow: ellipsis", text)
        # The removed fixed overlay must not leave dead spacers behind.
        self.assertNotIn("body.has-sb #view", text)


REAL_PAINT_FNS = [
    "paintGraph",
    "setGraphMsg",
    "setRelationHint",
    "setGraphLimits",
    "validGraphSel",
    "graphTruncated",
    "graphLimitsNotice",
    "graphFiltered",
    "graphEmptyKind",
    "graphSearchHits",
    "graphSearchPage",
    "paintSearchPager",
    "clearGraphSearch",
    "clearGraphFilters",
]

REAL_PAINT_STUBS = r'''
var graphCache = null, graphSel = null, gDetailKey = null;
var gQuery = "", gClientType = "", gClientStatus = "";
var gMode = "overview", gFocus = null;
var gDepMembers = {}, gAggMembers = {};
var gSearchPage = 0, gSearchPages = 1;
function syncHash() {}
function graphColor() { return "#000"; }
function graphCanvasW() { return 640; }
function setGraphZero() {}
function fitGraphChrome() {}
var gZeroNotice = false;
var gRelHintMsg = null, gRelHintEl = null;
var gLimitsMsg = null, gLimitsEl = null;
function mkEl(id) {
  return {
    id: id, innerHTML: "", textContent: "", value: "", onclick: null,
    dataset: {}, style: {}, _attrs: {},
    classList: { toggle: function () {}, add: function () {}, remove: function () {} },
    setAttribute: function (k, v) { this._attrs[k] = v; },
    getAttribute: function (k) { return (k in this._attrs) ? this._attrs[k] : null; },
    removeAttribute: function (k) { delete this._attrs[k]; },
    querySelector: function (sel) {
      if (!this._qs) this._qs = {};
      if (!this._qs[sel]) this._qs[sel] = mkEl(this.id + sel);
      return this._qs[sel];
    },
    querySelectorAll: function () { return []; }
  };
}
var __els = {};
function $(id) {
  if (!__els[id]) __els[id] = mkEl(id);
  return __els[id];
}
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function ok(c, msg) { eq(!!c, true, msg); }
function resetPaintHarness() {
  for (const k in __els) delete __els[k];
  graphCache = null; graphSel = null; gDetailKey = null;
  gQuery = ""; gClientType = ""; gClientStatus = "";
}
''';

REAL_PAINT_CHECKS = r'''
// A) search zero with a preexisting selection: the old inspector must not
//    erase the honest no-result message and its clear action.
resetPaintHarness();
graphCache = {nodes: [{id: "n1", type: "issue", label: "슬라이더 고장"}], edges: []};
gQuery = "zzz-no-match";
graphSel = "kept-sel";
paintGraph();
eq($("gzoom").innerHTML, "", "search-zero leaves the canvas empty");
ok($("gdetail").innerHTML.indexOf("일치하는 기록 없음") !== -1,
  "search-zero keeps the no-result message");
ok(typeof $("g-clearq").onclick === "function", "search-zero keeps a clear action");
eq($("gsub").textContent.indexOf("0건") !== -1, true, "search-zero hint shows the zero count");
eq(graphSel, "kept-sel", "selection identity survives a search-zero render");
eq($("gdetail").innerHTML.indexOf("g-scan") === -1, true, "search-zero never suggests a scan");
// B) hostile query text is escaped through the real escaper.
resetPaintHarness();
graphCache = {nodes: [{id: "n1", type: "issue", label: "a"}], edges: []};
gQuery = '"><img src=x onerror=alert(1)>';
graphSel = "kept-sel";
paintGraph();
ok($("gdetail").innerHTML.indexOf("&lt;img") !== -1, "hostile query is escaped");
ok($("gdetail").innerHTML.indexOf("<img src") === -1, "no raw markup is rendered");
eq(graphSel, "kept-sel", "selection identity survives an escaped render");
// C) type-filter zero: filter wording plus filter clear, never a scan CTA.
resetPaintHarness();
graphCache = {nodes: [{id: "n1", type: "issue", label: "a"}], edges: []};
gClientType = "run"; gQuery = "";
paintGraph();
ok($("gdetail").innerHTML.indexOf("필터와 일치") !== -1, "filtered-zero names the filter");
ok(typeof $("g-clearf").onclick === "function", "filtered-zero keeps a filter clear");
eq($("gdetail").innerHTML.indexOf("g-scan") === -1, true, "filtered-zero never suggests a scan");
// D) genuinely empty cache keeps the scan route.
resetPaintHarness();
graphCache = {nodes: [], edges: []};
paintGraph();
ok($("gdetail").innerHTML.indexOf("g-scan") !== -1, "true empty keeps the scan CTA");
console.log("PASS " + pass);
''';


def _decl(src, name):
    m = re.search(
        r"(?:^|\n)((?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\))",
        src,
    )
    assert m, f"missing function {name}"
    return m.group(1) + " " + _fn_body(src, name)


class GraphSearchZeroRenderTest(unittest.TestCase):
    def test_real_paint_search_zero_with_selection(self):
        src = _script()
        decls = "\n".join(_decl(src, n) for n in REAL_PAINT_FNS)
        type_line = next(
            line for line in src.splitlines()
            if line.startswith("const TYPE_KO = ")
        )
        esc_line = next(
            line for line in src.splitlines()
            if line.startswith("function esc(")
        )
        assert esc_line.rstrip().endswith("}"), "esc must stay a single-line declaration"
        page_line = next(
            line for line in src.splitlines()
            if line.startswith("const SEARCH_PAGE_N = ")
        )
        script = (
            REAL_PAINT_STUBS + "\n" + type_line + "\n" + esc_line + "\n"
            + page_line + "\n" + decls + "\n" + REAL_PAINT_CHECKS
        )
        proc = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout, f"render failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")


if __name__ == "__main__":
    unittest.main()
