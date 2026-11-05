"""48f UI regressions: focus, search scope, wording, phases, async restore.

Extracts actual helpers from the shipped ``src/openmainty/dashboard.html``
inline script and executes them in node against fake DOM/state. Helper
behavior is executed for real; call-site wiring (Escape handlers, group
layout, palette mapping) is asserted on the shipped source, and the full
Enter/Escape interaction itself is covered by actual Chrome QA, not claimed
here. Incidental formatters (``esc``) are stubbed following the existing
``test_work_ui_logic`` fixture approach.
"""

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src" / "openmainty" / "dashboard.html"

FUNCTIONS = [
    "fuzzy",
    "waitKo",
    "bareRunId",
    "chatHeadFor",
    "openChat",
    "paletteItems",
    "openPalette",
    "renderPalette",
    "paintPalette",
    "closePalette",
    "bundleEmptyHtml",
    "refocusGraphNode",
    "focusGraphCanvas",
    "clearGraphPanel",
    "gateLabel",
    "evRank",
    "batchPhaseKo",
    "batchNextAction",
    "sharedBlockerLine",
    "scrollerOf",
    "revQueueTop",
    "restoreRevQueueTop",
    "captureReviewState",
    "restoreReviewState",
    "trackChgLoad",
    "restoreReviewStateAsync",
    "stashReviewState",
    "routeKey",
    "routeScrollReset",
    "takeStashedReviewState",
    "restoreReadingAnchor",
    "graphContentSig",
    "graphSelAlive",
    "buildAggView",
    "graphRepoOf",
    "aggBranchSummary",
    "setGraphStatus",
    "setGraphWarning",
    "graphPollRefresh",
    "graphFetchDone",
    "gLabelW",
    "splitScopeLabel",
    "wrapLabelLines",
    "groupRowH",
    "expandedGroupLayout",
]

DRIVER = r'''
const fs = require("fs");
const html = fs.readFileSync(process.argv[1], "utf8");
const m = html.match(/<\/script>/);
if (!m) { console.log("FAIL no inline script"); process.exit(1); }
const src = html.slice(html.indexOf("<script>") + 8, m.index);
const wantFn = process.argv[2].split(",").filter(Boolean);
// Shipped module-level poll/generation state used by graphPollRefresh
// is spliced into the single evaluated program below (a separate eval
// would scope its `let` away). Extracted verbatim, not stubbed.

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
  // Exact declaration tokens only: no RegExp string escapes involved.
  const tags = ['\nasync function ' + name + '(', '\nfunction ' + name + '(',
    'async function ' + name + '(', 'function ' + name + '('];
  let idx = -1, start = 0;
  for (const t of tags) {
    idx = src.indexOf(t);
    if (idx >= 0) { start = idx + (t.charAt(0) === '\n' ? 1 : 0); break; }
  }
  if (idx < 0) throw new Error('missing function ' + name);
  const brace = src.indexOf('{', start);
  return src.slice(start, sliceBalanced(src, brace));
}
const stubs = `
function esc(s) { return String(s == null ? "" : s); }
var CSS = {escape: function (s) { return String(s).replace(/"/g, ""); }};
var document = {activeElement: null, body: {_body: true},
  scrollingElement: null, documentElement: null, _svg: null, _panel: null,
  getElementById: function (id) {
    if (id === "gsvg") return this._svg;
    if (id === "gdetail") return this._panel;
    return null;
  },
  querySelector: function () { return null; },
  querySelectorAll: function () { return []; }};
var view = {kind: "repo", id: "r"};
var repoTab = "reviews";
var lastRouteKey = null;
var O = {repos: [], runs: [], chats: []};
var fakeTabBox = null;
var fakePalInput = {value: "", _focused: null, oninput: null, onkeydown: null,
  focus: function () { this._focused = true; },
  setSelectionRange: function () {}};
var fakePaletteEl = {innerHTML: "",
  classList: {add: function () {}, remove: function () {}, contains: function () { return false; }}};
var __any = {};
function $(id) {
  if (id === "tabbody") return (fakeTabBox || {});
  if (id === "palette") return fakePaletteEl;
  if (id === "pal-input") return fakePalInput;
  // A real document returns the same node for the same id; the
  // product's poll guards compare DOM identity across awaits, so a
  // fresh object per call would reject every legitimate refresh.
  if (!__any[id]) __any[id] = {};
  return __any[id];
}
var palItems = [], palActive = 0;
function hlHtml(label, hl) { return "HL:" + label; }
function openRepo(n) { opened.push(["repo", n]); }
function openWork(i) { opened.push(["work", i]); }
function openAddRepo() { opened.push(["addrepo"]); }
function openSettings() { opened.push(["settings"]); }
var opened = [];
var gDetailKey = "stale";
var pendingRevState = null;
var renderCalls = 0;
function render() { renderCalls++; }
function closeSide() {}
var renderChatShell = {};
var chgLoads = new Map();
var hydrateResolve = null;
function loadBatchChanges(slot) {
  return new Promise(function (res) { hydrateResolve = res; });
}
var jgetResolve = null;
function jget(u) { return new Promise(function (res) { jgetResolve = res; }); }
var gNeedFit = false, gLayoutGen = 0, graphCache = null, graphSel = null;
var AGG_TYPES = ["dependency", "work", "run", "verification", "branch", "issue", "question"];
var AGG_KO = {dependency: "의존성"};
var BR_STATE_KO = {actual: "생성", planned: "예정", missing: "없음", unknown: "확인불가"};
var svgW = 316, paintCalls = 0;
function graphCssW() { return svgW; }
function graphContainerChanged(a, b) { return a !== b; }
function paintGraph() {
  paintCalls++;
  document.activeElement = document.body;
}
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function ok(c, msg) { eq(!!c, true, msg); }
function fakeBtn(key) {
  return {dataset: {k: key}, _focused: null,
    focus: function (o) { this._focused = o || {}; },
    closest: function () { return null; }};
}
function fakeChgBtn(key, path, top, slot) {
  return {dataset: {k: key, chgpath: path}, _hot: true, _top: top,
    _focused: null,
    focus: function (o) { this._focused = o || {}; },
    getBoundingClientRect: function () { return {top: this._top}; },
    closest: function () { return slot || null; }};
}
function fakeBox(o) {
  o = o || {};
  return {dataset: {}, _details: o.details || [], _btns: o.btns || {},
    _chg: o.chg || [], _slots: o.slots || {},
    parentElement: o.scroller || null,
    contains: function (el) { return Object.values(this._btns).indexOf(el) >= 0; },
    querySelectorAll: function (sel) {
      if (sel === "details[data-dk][open]") return this._details.filter(function (d) { return d.open; });
      if (sel === "details") return this._details;
      return [];
    },
    querySelector: function (sel) {
      const pre = '[data-k="';
      if (sel.slice(0, pre.length) === pre && sel.slice(-2) === '"]')
        return this._btns[sel.slice(pre.length, -2)] || null;
      const dpre = 'details[data-dk="';
      if (sel.slice(0, dpre.length) === dpre && sel.slice(-2) === '"]') {
        const k = sel.slice(dpre.length, -2);
        return this._details.find(function (d) { return d.dk === k; }) || null;
      }
      if (sel === "[data-chgpath].hot") {
        const all = Object.values(this._btns).concat(this._chg || []);
        return all.find(function (b) {
          return b && b.dataset && b.dataset.chgpath && b._hot; }) || null;
      }
      const cp = '[data-chgpath="';
      if (sel.slice(0, cp.length) === cp && sel.slice(-2) === '"]') {
        const p = sel.slice(cp.length, -2);
        const all = Object.values(this._btns).concat(this._chg || []);
        return all.find(function (b) {
          return b && b.dataset && b.dataset.chgpath === p; }) || null;
      }
      const sp = '[data-changes="';
      if (sel.slice(0, sp.length) === sp && sel.slice(-2) === '"]') {
        return (this._slots || {})[sel.slice(sp.length, -2)] || null;
      }
      return null;
    }};
}
function fakeDetail(dk, open, slotKey) {
  return {dk: dk, dataset: {dk: dk}, open: !!open,
    querySelector: function () {
      return slotKey ? {dataset: {changes: slotKey}} : null;
    }};
}
`;

const tests = `
;(async () => {
// 1) graph keyboard helpers: picked node regains focus; escape path clears
//    the stale panel and returns to the canvas (handlers wired in source)
document._svg = {querySelector: function (sel) {
  return sel === '[data-gid="issue:r#774"]' ? fakeBtn("n") : null; }};
eq(refocusGraphNode("issue:r#774"), true, "picked node refocuses");
eq(refocusGraphNode("missing"), false, "missing node fails quietly, no throw");
eq(refocusGraphNode(""), false, "empty gid never focuses");
const el = (function () {
  let got = null;
  document._svg.querySelector = function () { got = fakeBtn("n"); return got; };
  refocusGraphNode("n1");
  return got; })();
eq(JSON.stringify(el._focused), JSON.stringify({preventScroll: true}),
  "refocus does not move scroll");
document._svg.focused = null;
document._svg.focus = function (o) { this.focused = o || {}; };
focusGraphCanvas();
eq(JSON.stringify(document._svg.focused), JSON.stringify({preventScroll: true}),
  "escape path returns focus to the canvas");
document._panel = {innerHTML: "stale 56 members"};
clearGraphPanel();
eq(gDetailKey, null, "escape clears the panel key");
eq(document._panel.innerHTML, "노드를 클릭하면 근거와 함께 표시됩니다.",
  "escape replaces the stale bundle with default guidance");

// 2) empty bundle: titled empty state + clear-search, never a raw agg id
const eb1 = bundleEmptyHtml(true);
ok(eb1.indexOf("현재 보기에 없습니다") >= 0, "empty bundle has a title");
ok(eb1.indexOf("검색 해제") >= 0, "empty bundle offers clear-search");
ok(eb1.indexOf("agg:") < 0, "no raw aggregate id shown");
ok(eb1.indexOf("묶음 펼치기") < 0, "no meaningless expand button");
const eb0 = bundleEmptyHtml(false);
ok(eb0.indexOf("현재 보기에 없습니다") >= 0, "no-filter empty keeps its title");
ok(eb0.indexOf("gd-clearq") < 0, "no clear button without a query");

// 3) search covers every run and chat with the ACTUAL data shape
O.repos = [{name: "r", github: "o/r"}];
O.runs = [];
for (let i = 0; i < 10; i++) O.runs.push(
  {id: "run" + i, repo: "r", issue: String(700 + i),
   title: i === 9 ? "아홉 번째 실행 766 관련" : "실행 " + i, waitReason: ""});
O.chats = [{id: "chat-f92e5ea4", title: "답글 초안 iOS 릴리스 녹음 설정 실패",
  issue: "766", repo: "react-native-nitro-sound"},
  {id: "chat-other", title: "주간 정리", issue: "", repo: "r"}];
const hits = paletteItems("766");
const chatHit = hits.find(function (it) { return it.group === "대화"; });
ok(!!chatHit, "chat found by issue though its title has no number");
ok(chatHit.sub.indexOf("#766") >= 0, "chat reference stays secondary");
eq(chatHit.hl, null, "no invalid title highlight for a metadata-only match");
ok(hits.some(function (it) { return it.group === "실행 기록" && it.label.indexOf("아홉 번째") === 0; }),
  "run past the old slice(0,8) window is found");
const runHit = hits.find(function (it) { return it.group === "실행 기록" && it.label.indexOf("아홉 번째") === 0; });
ok(runHit.hl && runHit.hl.every(function (p) { return p >= 0 && p < runHit.label.length; }),
  "title-match highlight stays inside the label");
eq(runHit.label.slice(runHit.hl[0], runHit.hl[0] + 1), "7",
  "highlight starts on the real matched character");
chatHit.run();
eq(view.kind, "chat", "chat result opens the chat view");
eq(view.id, "chat-f92e5ea4", "chat result opens the exact chat");
const emptyQ = paletteItems("");
ok(emptyQ.every(function (it) { return it.group !== "대화"; }),
  "empty query keeps the old recent-only scope, no chats");
eq(emptyQ.filter(function (it) { return it.group === "실행 기록"; }).length, 8,
  "empty query still shows only recent runs");
const titled = paletteItems("아홉 번째").find(function (it) { return it.group === "실행 기록"; });
ok(titled && titled.label.indexOf("아홉 번째") === 0, "run label leads with the title");
ok(titled && titled.sub.indexOf("r#709") === 0, "repo reference stays secondary");

// 4) human wording: no raw gate ids, no duplicated counts
eq(gateLabel({policy: {gates: {"root-qa": {label: "Root 결정 QA"}}}}, "root-qa"),
  "최종 검수 확인", "internal verdict name never faces the user");
eq(gateLabel({policy: {gates: {unit: {label: "단위 테스트"}}}}, "unit"),
  "단위 테스트", "catalog labels still used");
eq(gateLabel({}, "mystery"), "mystery", "unknown gate falls back to its key");
eq(evRank({stored_path: "a/shot.png"}), 0, "screenshot first");
eq(evRank({stored_path: "a/results.xml"}), 1, "junit xml before debug json");
eq(evRank({stored_path: "a/review.json"}), 2, "debug json last");

// 5) batch phase and next action derive from real records
const full = {primaries: 8, ready: 8, attention: [],
  integrations: [{lane: "main", status: "integration-testing",
    gates: [{key: "unit", status: "pass"}, {key: "root-qa", status: "pass"}]}]};
eq(batchPhaseKo(full), "전 항목 검증 완료", "all-ready batch says so");
eq(batchNextAction(full), "전 항목 검증 완료 · 병합 검토 가능",
  "all-ready next action never says Root QA");
const stuck = {primaries: 8, ready: 3,
  attention: [{key: "g1", label: "Android 실제 실행"}],
  integrations: [{lane: "main", gates: [{key: "native-android", status: "pending"}]}]};
eq(batchPhaseKo(stuck), "막힘 해소 중: Android 실제 실행",
  "blocker comes from stored attention");
const prog = {primaries: 8, ready: 3, attention: [],
  policy: {gates: {"root-qa": {label: "Root 결정 QA"}}},
  integrations: [{lane: "main",
    gates: [{key: "unit", status: "pass"}, {key: "root-qa", status: "pending"}]}]};
eq(batchPhaseKo(prog), "게이트 진행 중 · 대기: 최종 검수 확인",
  "pending gates named in human words");
eq(batchPhaseKo({primaries: 0, ready: 0, attention: [], integrations: []}),
  "게이트 진행 중", "no data never invents progress");
eq(batchNextAction(stuck), "막힘 해소: Android 실제 실행", "attention drives next action");
const line = sharedBlockerLine([
  {id: "a", purpose: "A 묶음", primaries: 8, ready: 8, attention: [],
   inherited: [], integrations: []},
  {id: "b", purpose: "B 묶음", primaries: 5, ready: 0,
   attention: [{key: "unit", label: "단위 테스트"}],
   inherited: [{batch: "a", group: "", count: 8}], integrations: []}]);
ok(line.indexOf("검증 완료") >= 0, "proven companion reads complete");
ok(line.indexOf("동반 대기") < 0, "no waiting claim when all 15 are ready");
const line2 = sharedBlockerLine([
  {id: "a", purpose: "A 묶음", primaries: 8, ready: 3,
   attention: [{key: "unit", label: "단위 테스트"}], inherited: [], integrations: []},
  {id: "b", purpose: "B 묶음", primaries: 5, ready: 0, attention: [],
   inherited: [{batch: "a", group: "", count: 8}], integrations: []}]);
ok(line2.indexOf("동반 대기") >= 0, "unproven companion still waits, honestly");

// 6) real-change poll: stash before the wipe, restore after the rebuild
const scroller = {scrollHeight: 200, clientHeight: 100, scrollTop: 55};
const oldBtn = fakeBtn("chg-x");
const oldBox = fakeBox({scroller: scroller,
  details: [fakeDetail("b-chg-main", true, "b|main")], btns: {"chg-x": oldBtn}});
document.activeElement = oldBtn;
fakeTabBox = oldBox;
eq(stashReviewState(), true, "pre-wipe stash works");
const st = takeStashedReviewState();
eq(st.key, "chg-x", "stashed focus key survives the wipe");
eq(st.top, 55, "stashed scroll survives the wipe");
eq(takeStashedReviewState(), null, "stash is one-shot");
view.id = "other";
fakeTabBox = oldBox;
stashReviewState();
view.id = "r";
eq(takeStashedReviewState(), null, "repo switch discards the stash");
const newBtn = fakeBtn("chg-x");
const newBox = fakeBox({scroller: scroller,
  details: [fakeDetail("b-chg-main", false, "b|main")], btns: {"chg-x": newBtn}});
document.activeElement = document.body;
restoreReviewState(newBox, st);
eq(scroller.scrollTop, 55, "rebuild restores scroll, never 0");
eq(newBox._details[0].open, true, "rebuild reopens the evidence");
eq(JSON.stringify(newBtn._focused), JSON.stringify({preventScroll: true}),
  "rebuild restores focus without moving scroll");

// 7) delayed hydration: focus follows the real render, never steals a move
const hBtn = fakeBtn("chg-pkg");
const hBox = fakeBox({scroller: {scrollHeight: 200, clientHeight: 100, scrollTop: 0},
  details: [fakeDetail("b-chg-main", true, "b|main")], btns: {}});
document.activeElement = document.body;
const hst = {key: "chg-pkg", open: ["b-chg-main"], top: 0};
const hp = restoreReviewStateAsync(hBox, hst);
await new Promise(function (r) { setTimeout(r, 0); });
hBox._btns["chg-pkg"] = hBtn;
hydrateResolve();
await hp;
eq(JSON.stringify(hBtn._focused), JSON.stringify({preventScroll: true}),
  "focus lands after the delayed target actually renders");
const hBtn2 = fakeBtn("chg-pkg");
const hBox2 = fakeBox({details: [fakeDetail("b-chg-main", true, "b|main")], btns: {}});
const hp2 = restoreReviewStateAsync(hBox2, hst);
await new Promise(function (r) { setTimeout(r, 0); });
const external = fakeBtn("global-search");
document.activeElement = external;
hBox2._btns["chg-pkg"] = hBtn2;
hydrateResolve();
await hp2;
eq(hBtn2._focused, null, "no steal once the user moved to a global box");
const sBtn = fakeBtn("chg-x");
const sBox = fakeBox({details: [], btns: {"chg-x": sBtn}});
document.activeElement = external;
restoreReviewState(sBox, {key: "chg-x", open: [], top: 0});
eq(sBtn._focused, null, "sync restore never steals a real focus");

// 8) content signature: facts repaint, decoration never does
const g1 = {nodes: [{id: "n", type: "run", label: "L", status: "done"}],
  edges: [{from: "a", to: "n", kind: "about"}]};
const gStatus = {nodes: [{id: "n", type: "run", label: "L", status: "failed"}],
  edges: [{from: "a", to: "n", kind: "about"}]};
const gProv = {nodes: [{id: "n", type: "run", label: "L", status: "done"}],
  edges: [{from: "a", to: "n", kind: "about", provenance: "scan row x"}]};
const decorated = JSON.parse(JSON.stringify(g1));
decorated.nodes[0]._x = 92; decorated.nodes[0]._label = "L";
decorated.nodes[0]._lines = ["L"]; decorated.nodes[0]._halfH = 22;
ok(graphContentSig(g1) !== graphContentSig(gStatus), "status flip changes the signature");
ok(graphContentSig(g1) !== graphContentSig(gProv), "provenance change changes the signature");
eq(graphContentSig(g1), graphContentSig(decorated), "render decoration never fakes a change");
eq(graphContentSig(g1), graphContentSig(JSON.parse(JSON.stringify(g1))), "identical facts stay quiet");
view.kind = "graph";
graphCache = decorated; graphSel = "n"; paintCalls = 0; gNeedFit = false;
// L1 cool: the polled repo is "" here, so the cache holder matches it;
// each independent case below starts outside the cool window (>240 s
// passing, or a fresh view), modelled by an explicit reset.
graphRepo = "";
gPollCoolUntil = 0;
document.activeElement = document.body;
document._svg = {querySelector: function () { return fakeBtn("n"); }};
let p = graphPollRefresh();
await new Promise(function (r) { setTimeout(r, 0); });
jgetResolve(gStatus);
await p;
eq(paintCalls, 1, "same-count status change repaints");
eq(graphCache, gStatus, "cache follows the fresh facts");
paintCalls = 0;
gPollCoolUntil = 0;
p = graphPollRefresh();
await new Promise(function (r) { setTimeout(r, 0); });
jgetResolve(gStatus);
await p;
eq(paintCalls, 0, "quiet poll with same facts never repaints");
eq(graphSel, "n", "quiet poll keeps the selection");
eq(gNeedFit, false, "quiet poll keeps the zoom");
// A tick inside the cool window issues no fetch and disturbs nothing.
jgetResolve = null;
const pc = graphPollRefresh();
await new Promise(function (r) { setTimeout(r, 0); });
eq(jgetResolve, null, "cooled tick issues no fetch");
await pc;
eq(paintCalls, 0, "cooled tick never repaints");
eq(graphSel, "n", "cooled tick keeps the selection");

// 9) full names wrap without ellipsis; rows grow so siblings never overlap
const avail390 = 200;
const scoped = "@react-native-community/cli-platform-android";
const wl = wrapLabelLines(scoped, avail390);
eq(wl.join("").replace(/\\//g, "/"), scoped, "wrap keeps every character");
ok(wl.length > 1, "long scoped name wraps");
ok(wl.every(function (ln) { return gLabelW(ln) <= avail390; }), "every line fits the 390 column");
ok(wl.every(function (ln) { return ln.indexOf("…") < 0; }), "no ellipsis in wrapped lines");
const unscoped = "react-native-builder-bob-long-package-name";
const wu = wrapLabelLines(unscoped, 100);
eq(wu.join(""), unscoped, "unscoped long name keeps every character");
ok(wu.every(function (ln) { return gLabelW(ln) <= 100; }), "hard-broken lines fit");
eq(wrapLabelLines("del-cli", avail390).join(","), "del-cli", "short names stay one line");
eq(groupRowH(1), 62, "one line keeps the base row");
eq(groupRowH(2), 62, "two lines keep the base row");
ok(groupRowH(3) >= 69 && groupRowH(4) > groupRowH(3), "extra lines grow the row");
const lay = expandedGroupLayout(["a", "b", "c"], 92, 100, 62, [62, 69, 62]);
eq(lay[0].y, 100, "variable layout starts at the top");
eq(lay[1].y, 162, "second row starts after the first height");
eq(lay[2].y, 231, "third row starts after the grown second row");
const layFixed = expandedGroupLayout(["a", "b"], 92, 100, 62);
eq(layFixed[1].y, 162, "fixed layout keeps its old behavior");

// 11) aggregate poll: a valid bundle selection survives a same-count
//     semantic poll with focus; a dead one clears truthfully with panel
const aggBase = {nodes: [
    {id: "repo:r", type: "repo", label: "r"},
    {id: "dep:react", type: "dependency", label: "react", status: "done", repo: "r"},
    {id: "dep:vue", type: "dependency", label: "vue", status: "done", repo: "r"}],
  edges: []};
const AGGID = "agg:r:dependency";
ok(graphSelAlive(AGGID, aggBase), "bundle with members is alive");
ok(graphSelAlive("dep:react", aggBase), "raw ids stay alive by identity");
ok(!graphSelAlive(AGGID, {nodes: [{id: "repo:r", type: "repo", label: "r"}],
  edges: []}), "bundle without members is dead");
ok(!graphSelAlive("depgrp:r", aggBase), "non-agg derived ids never match raw");
ok(!graphSelAlive("", aggBase), "empty selection is dead");
// same-count member status flip: selection kept, repaint, focus follows
const aggFlip = JSON.parse(JSON.stringify(aggBase));
aggFlip.nodes[1].status = "failed";
view.kind = "graph";
graphCache = aggBase; graphSel = AGGID; paintCalls = 0; gNeedFit = false;
graphRepo = "";
gPollCoolUntil = 0;
const memberBtn = fakeBtn("member");
memberBtn.dataset.gid = "dep:react";
document.activeElement = memberBtn;
document._svg = {querySelector: function (sel) {
  return sel === '[data-gid="dep:react"]' ? memberBtn : null; }};
document._panel = {innerHTML: "bundle facts"};
p = graphPollRefresh();
await new Promise(function (r) { setTimeout(r, 0); });
jgetResolve(aggFlip);
await p;
eq(paintCalls, 1, "same-count fact change repaints with a bundle selected");
eq(graphSel, AGGID, "valid bundle selection survives the poll");
eq(JSON.stringify(memberBtn._focused), JSON.stringify({preventScroll: true}),
  "focused member node keeps focus, not body");
eq(gNeedFit, false, "poll keeps the zoom");
// members gone: selection clears and the inspector clears with it
const aggGone = {nodes: [
    {id: "repo:r", type: "repo", label: "r"},
    {id: "issue:9", type: "issue", label: "nine", repo: "r"},
    {id: "issue:10", type: "issue", label: "ten", repo: "r"}],
  edges: []};
graphCache = aggBase; graphSel = AGGID; paintCalls = 0;
graphRepo = "";
gPollCoolUntil = 0;
document.activeElement = document.body;
document._panel = {innerHTML: "stale bundle facts"};
p = graphPollRefresh();
await new Promise(function (r) { setTimeout(r, 0); });
jgetResolve(aggGone);
await p;
eq(paintCalls, 1, "dead bundle still repaints fresh facts");
eq(graphSel, null, "dead bundle selection clears");
eq(document._panel.innerHTML, "노드를 클릭하면 근거와 함께 표시됩니다.",
  "dead bundle clears the inspector with it");

// 10) reopen consistency: a stale query never lingers next to fresh results
fakePalInput.value = "766";
palItems = paletteItems("766");
openPalette();
eq(fakePalInput.value, "", "reopen clears the stale input");
ok(palItems.every(function (it) { return it.group !== "대화"; }),
  "reopen shows the fresh recent scope");
fakePalInput.value = "766";
fakePalInput.oninput();
eq(fakePalInput.value, "766", "typing keeps its query");
ok(palItems.some(function (it) { return it.group === "대화"; }),
  "typing recomputes the matching scope");
const keptItems = palItems;
fakePalInput.onkeydown({key: "ArrowDown", preventDefault: function () {}});
eq(fakePalInput.value, "766", "arrow repaint keeps the query");
eq(palItems, keptItems, "arrow repaint keeps the results");

// 12) outer rebuild reading anchor: the selected file returns to its
//     viewport position after async hydration, unless the user moved
const scR = {scrollHeight: 9000, clientHeight: 800, scrollTop: 5717};
const oldSlot = {dataset: {changes: "b|main"}, parentElement: scR};
const oldHot = fakeChgBtn("chg-b-18", "package.json", 521.83, oldSlot);
const oldBoxR = fakeBox({scroller: scR,
  details: [fakeDetail("b-chg-main", true, "b|main")],
  btns: {"chg-b-18": oldHot}});
// focused-file wiring case: focus was on the old file button itself
document.activeElement = oldHot;
view.id = "r"; repoTab = "reviews";
const keepR = captureReviewState(oldBoxR);
eq(keepR.anchor && keepR.anchor.path, "package.json", "anchor captures the file");
eq(keepR.anchor && keepR.anchor.slot, "b|main", "anchor captures the slot");
eq(keepR.anchor && keepR.anchor.viewTop, 521.83, "anchor captures the viewport top");
eq(keepR.key, "chg-b-18", "capture keeps the focused file key");
const newSlot = {dataset: {changes: "b|main"}, parentElement: scR,
  querySelector: function (sel) {
    const cp = '[data-chgpath="';
    if (sel.slice(0, cp.length) === cp && sel.slice(-2) === '"]')
      return sel.slice(cp.length, -2) === "package.json" ? newHot : null;
    return null;
  }};
const newHot = fakeChgBtn("chg-b-18", "package.json", 206.03, newSlot);
const newBoxR = fakeBox({scroller: scR,
  details: [fakeDetail("b-chg-main", false, "b|main")],
  btns: {"chg-b-18": newHot}, slots: {"b|main": newSlot}});
// rebuild drops focus to body, like a real DOM replacement
document.activeElement = document.body;
const rpR = restoreReviewStateAsync(newBoxR, keepR);
await new Promise(function (r) { setTimeout(r, 0); });
hydrateResolve();
await rpR;
eq(JSON.stringify(newHot._focused), JSON.stringify({preventScroll: true}),
  "replacement file button regains focus by stable key");
ok(Math.abs(scR.scrollTop - 5401.2) < 0.01,
  "anchor still corrects after the focused replacement, scroll otherwise kept");
// user scrolled meanwhile: no touch
scR.scrollTop = 5717;
document.activeElement = document.body;
eq(restoreReadingAnchor(newSlot, keepR.anchor), true, "anchor restores again");
scR.scrollTop = 5800;
eq(restoreReadingAnchor(newSlot, keepR.anchor), false, "user scroll cancels");
eq(scR.scrollTop, 5800, "cancelled restore never moves scroll");
// user moved focus meanwhile: no touch, no steal
// (reset the legitimate focus from the main flow above first)
newHot._focused = null;
scR.scrollTop = 5717;
document.activeElement = external;
eq(restoreReadingAnchor(newSlot, keepR.anchor), false, "moved focus cancels");
eq(newHot._focused, null, "cancelled restore never focuses");

// 13) route vs poll scroll: explicit navigation to a new route identity
//     resets (render scrolls to top); same-route data polls keep reading state
lastRouteKey = null;
view = {kind: "repo", id: "r"}; repoTab = "reviews";
eq(routeScrollReset(), true, "first render starts at top");
eq(routeScrollReset(), false, "same-route poll keeps reading state");
view = {kind: "command"};
eq(routeScrollReset(), true, "repo detail -> command center starts at top");
eq(routeScrollReset(), false, "command poll keeps scroll");
view = {kind: "repo", id: "r"}; repoTab = "reviews";
eq(routeScrollReset(), true, "command -> repo returns to top");
repoTab = "deps";
eq(routeScrollReset(), true, "same repo tab switch starts at top");
eq(routeScrollReset(), false, "deps poll keeps scroll");
view = {kind: "graph", repo: "r"}; repoTab = "reviews";
eq(routeScrollReset(), true, "graph repo switch starts at top");
eq(routeScrollReset(), false, "graph poll keeps zoom/scroll");
console.log("PASS " + pass);
})();
`;

const pieces = [stubs];
const gstate = src.split("\n").filter(function (l) {
  return l.indexOf("let gGraphGen") === 0 || l.indexOf("const G_STALE_WARN =") === 0 ||
    l.indexOf("let graphRepo") === 0 || l.indexOf("let gGraphFetch") === 0 ||
    l.indexOf("const G_POLL_COOL_MS =") === 0;
});
if (gstate.length !== 5) { console.log("FAIL eval :: missing graph state lines"); process.exit(1); }
pieces.push(gstate.join("\n"));
for (const n of wantFn) pieces.push(extractFn(n));
pieces.push(tests);
try {
  eval(pieces.join("\n"));
} catch (e) { console.log("FAIL eval :: " + (e && e.message)); process.exit(1); }
'''


def _run_node(extra_args):
    return subprocess.run(
        ["node", "-e", DRIVER, str(HTML), ",".join(FUNCTIONS)] + extra_args,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _script():
    text = HTML.read_text()
    blocks = re.findall(r"<script>(.*?)</script>", text, re.S)
    assert len(blocks) == 1, "expected one inline dashboard script"
    return blocks[0]


class Review48fUiLogicTest(unittest.TestCase):
    def test_review48f_ui_logic(self):
        proc = _run_node([])
        self.assertEqual(
            proc.returncode, 0, f"node driver failed: {proc.stdout}\n{proc.stderr}"
        )
        self.assertNotIn("FAIL", proc.stdout, f"logic failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, f"no passing assertions: {proc.stdout}")

    def test_review48f_wiring(self):
        src = _script()
        # helper behavior above is executed; these assert the call sites that
        # actual Chrome covers end to end (Escape/pick/clear/palette/layout)
        self.assertGreaterEqual(
            src.count("refocusGraphNode(gid)"), 2,
            "pick + focus paths must return focus to the node",
        )
        self.assertIn("clearGraphPanel(); paintGraph(); focusGraphCanvas()", src)
        self.assertIn("bundleEmptyHtml(!!gQuery)", src)
        self.assertIn("takeStashedReviewState() || captureReviewState(box)", src)
        self.assertIn("stashReviewState(); render()", src)
        self.assertNotIn("`준비 ${readyN}/${totalN}건` + (blockLabels", src)
        self.assertIn("batchPhaseKo(b)", src)
        self.assertIn("chatHeadFor(s, O.runs || [])", src)
        self.assertIn("s.run_id ||", src)
        self.assertIn("lm ? lm.hl : null", src)
        self.assertIn("wrapLabelLines(complete, side.avail)", src)
        self.assertIn('renderPalette("", "")', src)
        self.assertIn("lastRenderResetScroll = routeScrollReset()", src)
        self.assertIn("if (lastRenderResetScroll && $(\"view\")) {", src)
        self.assertIn(
            'if ($("view") && !lastRenderResetScroll) $("view").scrollTop = savedScroll;',
            src,
        )

    def test_initial_shell_accessible(self):
        text = HTML.read_text()
        self.assertIn('aria-label="관제 화면 불러오는 중"', text)
        self.assertIn("연결 확인 중", text)
