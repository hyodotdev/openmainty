"""Slice-1 shell regressions: distinct card titles, capped lists, theme hooks.

The dashboard's pure helpers are extracted from the shipped
``src/openmainty/dashboard.html`` inline script and executed with node, so
the test verifies actual logic rather than mirroring static strings.
No network, no app import, no HOME needed.
"""

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src" / "openmainty" / "dashboard.html"

FUNCTIONS = ["esc", "batchCardTitle", "splitHeadRest", "prBotLines",
             "applyThemeLabel", "selectOnly", "toggleSingle",
             "revQueueTop", "restoreRevQueueTop", "guardAlertHtml",
             "glanceRepoKey", "glanceOpenKeys", "restoreGlanceOpen",
             "pendingChip", "prepRegInfo", "primaryKey"]

DRIVER = r'''
const fs = require("fs");
const html = fs.readFileSync(process.argv[1], "utf8");
const m = html.match(/<script>([\s\S]*)<\/script>/);
if (!m) { console.log("FAIL no inline script"); process.exit(1); }
const src = m[1];
const wantFn = process.argv[2].split(",");
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
/* esc contains a regex literal with quotes that defeats brace counting,
   so take its single line directly (same approach as test_work_ui_logic). */
const escSrc = (src.match(/\nfunction esc\(s\) \{[^\n]*\}/) || [])[0] || "";
if (!escSrc) throw new Error("missing function esc");
const pieces = [escSrc.trim()];
for (const n of wantFn) { if (n !== "esc") pieces.push(extractFn(n)); }
/* Negative guard: no extracted helper may carry page-boot timers or
   listeners. A swallow here fails loudly instead of hanging node. */
for (const p of pieces) {
  if (/setInterval|setTimeout|addEventListener|localStorage/.test(p))
    throw new Error("extraction swallowed page boot code");
}
// Minimal DOM stubs so the real theme function runs its actual branches.
const __fakeBtn = {textContent: "", attrs: {},
  setAttribute(k, v) { this.attrs[k] = v; }};
function $(id) { if (id === "themebtn") return __fakeBtn; return null; }
const document = {documentElement: {dataset: {}}};
eval(pieces.join("\n"));
''' + r'''
let pass = 0;
function ok(c, label) { if (!c) { console.log("FAIL " + label); process.exit(1); } pass++; }
function eq(a, b, label) { if (a !== b) { console.log("FAIL " + label + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exit(1); } pass++; }
// batchCardTitle: real package/version leads; same consumer label stays distinguishable.
eq(batchCardTitle({purpose: "자동 등록…", consumer_label: "소비 스크립트 build:web 통과 (6초)",
    primary_items: [{dep: "turbo", target: "2.11.10", lane: "next", run_id: "run-5720a20b"}]}),
  "turbo 2.11.10", "primary dep+target leads");
const sameConsumer = {purpose: "자동 등록…", consumer_label: "소비 스크립트 build:web 통과 (6초)"};
const tA = batchCardTitle(Object.assign({primary_items: [{dep: "webpack", target: "5.112.1"}]}, sameConsumer));
const tB = batchCardTitle(Object.assign({primary_items: [{dep: "turbo", target: "2.11.10"}]}, sameConsumer));
ok(tA !== tB && tA === "webpack 5.112.1" && tB === "turbo 2.11.10",
  "same consumer label, different deps stay distinguishable");
eq(batchCardTitle({purpose: "자동 등록…", consumer_label: "Turbo Web 작업 실행 검증 통과 (11초)",
    primaries: 2, primary_items: [{dep: "turbo", target: "2.11.10"}, {dep: "expo", target: "58.0.19"}]}),
  "turbo 2.11.10 외 1건", "multi primary groups honestly");
eq(batchCardTitle({purpose: "자동 등록…", primaries: 70,
    primary_items: [{dep: "turbo", target: "2.11.10"}]}),
  "turbo 2.11.10 외 69건", "truncated projection still reports honest total");
const hostile = batchCardTitle({primary_items: [{dep: "<img src=x>", target: "<script>alert(1)</script>"}]});
const hostileUsed = esc(hostile);
ok(!hostileUsed.includes("<img") && !hostileUsed.includes("<script>") &&
  hostileUsed.includes("&lt;img"), "hostile dep escaped at the esc() use site");
eq(batchCardTitle({purpose: "자동 등록…", pod_drift: {pod: "React-Core-prebuilt", target: "0.88.0"}}),
  "iOS Podfile.lock 갱신 필요 — React-Core-prebuilt → 0.88.0", "pod drift title");
eq(batchCardTitle({purpose: "자동 등록…", newer_auto: "auto-prep-run-25dd835d-x"}),
  "더 새 후보로 대체: auto-prep-run-25dd835d-x", "newer auto title");
eq(batchCardTitle({purpose: "자동 등록…", joint_superseded_by: "prep-run-79bbc781-joint"}),
  "함께 올림으로 대체: prep-run-79bbc781-joint", "joint supersede title");
eq(batchCardTitle({purpose: "ESLint 플러그인·파서 8.71.2 호환성 검증"}),
  "ESLint 플러그인·파서 8.71.2 호환성 검증", "plain purpose kept");
eq(batchCardTitle({}), "검토 묶음", "empty batch fallback");
// typed issue list titles: real number/lane leads; mixed valid+malformed stays honest.
eq(batchCardTitle({repo: "r", primaries: 1,
    primary_items: [{issue: {issue: "856", repo: "r", lane: "next"}, lane: "next", run_id: "run-x"}]}),
  "이슈 #856 (next)", "issue title leads with number+lane");
eq(batchCardTitle({repo: "r", primaries: 2,
    primary_items: [{issue: {issue: "856", repo: "r", lane: "next"}, lane: "next", run_id: "run-x"},
      {dep: "turbo", target: "2.11.10", lane: "next", run_id: "run-y"}]}),
  "이슈 #856 (next) 외 1건", "issue+dep mixed total honest");
// primaryKey: typed issue namespace vs legacy dep key; mixed falls to issue display.
eq(primaryKey("r", {issue: {issue: "856", repo: "r", lane: "next"}, lane: "next", run_id: "run-x"}),
  "issue:r:next:856", "issue primary key namespaced");
eq(primaryKey("r", {dep: "turbo", target: "2.11.10", lane: "next"}),
  "r|turbo|2.11.10|next", "legacy dep key unchanged");
eq(primaryKey("r", {}), "r|||", "empty primary falls back, never throws");
// prepRegInfo: register button shows only for typed issue prep + usable receipt, stateless.
ok(prepRegInfo({op_id: "o1", issue: {issue: "856"}, fastcheck_summary: {steps: [{rc: 0}]}, gates: []}) !== null,
  "issue prep + executed receipt offers register");
eq(prepRegInfo({op_id: "o1", fastcheck_summary: {steps: [{rc: 0}]}, gates: []}), null,
  "dep prep offers no register");
eq(prepRegInfo({op_id: "o1", issue: {issue: "856"}, fastcheck_summary: {steps: []}, gates: []}), null,
  "empty receipt offers no register");
eq(prepRegInfo(null), null, "missing prep offers no register");
// splitHeadRest: first three visible, rest folded, total honest.
const cut = splitHeadRest([1, 2, 3, 4, 5]);
eq(JSON.stringify(cut.head), "[1,2,3]", "head is first three");
eq(JSON.stringify(cut.rest), "[4,5]", "rest is remainder");
eq(cut.total, 5, "total counts all");
const short = splitHeadRest(["a"]);
eq(short.rest.length, 0, "short list has no rest");
eq(splitHeadRest(null).total, 0, "null is empty");
// prBotLines: escaping kept, overflow folds into details, nothing dropped.
const evil = {open: 12, external: 1, bots: 5, bot_lines: [
  {pr: 1, dep: "<img src=x onerror=1>", note: "bump <script>alert(1)</script>"},
  {pr: 2, dep: "b"}, {pr: 3, dep: "c"}, {pr: 4, dep: "d"}, {pr: 5, dep: "e"}]};
const evilHtml = prBotLines(evil);
ok(!evilHtml.includes("<script>") && !evilHtml.includes("<img"), "bot lines escape hostile text");
ok(evilHtml.includes("&lt;script&gt;"), "escaped text visible");
ok(evilHtml.includes("<details"), "overflow folds into details");
ok(evilHtml.includes("더 보기 (2개)"), "fold names the hidden count");
for (const d of ["bump", " · b<", " · c<", " · d<", " · e<"]) ok(evilHtml.includes(d), "no bot line dropped: " + d);
const one = prBotLines({bot_lines: [{pr: 843, dep: "compression"}]});
ok(!one.includes("<details"), "short list has no fold");
// selectOnly/toggleSingle: one shared selection, deep link replaces.
const sel = new Set(["old-id"]);
eq(selectOnly(sel, "new-id"), "new-id", "deep link returns new id");
eq(sel.size, 1, "previous selection never retained");
ok(sel.has("new-id"), "new selection present");
eq(selectOnly(sel, ""), "", "empty clears");
eq(sel.size, 0, "empty leaves nothing selected");
const tog = new Set();
eq(toggleSingle(tog, "a"), "a", "first toggle selects");
eq(toggleSingle(tog, "b"), "b", "second id replaces first");
eq(tog.size, 1, "only one selection at a time");
ok(tog.has("b") && !tog.has("a"), "old id dropped");
eq(toggleSingle(tog, "b"), "", "same row toggles closed");
eq(tog.size, 0, "toggle off empties");
// revQueueTop/restoreRevQueueTop: nested queue position survives rerender.
const qbox = {querySelector: (s) => s === ".revqueue" ? {scrollTop: 360} : null};
eq(revQueueTop(qbox), 360, "reads nested queue position");
const qbox2 = {querySelector: (s) => s === ".revqueue" ? {scrollTop: 0} : null};
ok(restoreRevQueueTop(qbox2, 360) === true, "restores into queue");
eq(qbox2.querySelector(".revqueue").scrollTop, 0, "fake holds its own value");
const live = {scrollTop: 0};
const qbox3 = {querySelector: (s) => s === ".revqueue" ? live : null};
ok(restoreRevQueueTop(qbox3, 360) === true, "live queue updated");
eq(live.scrollTop, 360, "scroll position actually written");
eq(revQueueTop({querySelector: () => null}), null, "absent queue reads null");
eq(revQueueTop(null), null, "null box reads null");
ok(restoreRevQueueTop({querySelector: () => null}, 360) === false, "absent queue skips");
ok(restoreRevQueueTop(qbox3, "x") === false, "non-number top ignored");
ok(restoreRevQueueTop(qbox3, undefined) === false, "missing top ignored");
// guardAlertHtml: explicit failure surfaces, unknown stays silent.
eq(guardAlertHtml(null), "", "null system is unknown");
eq(guardAlertHtml({}), "", "missing m2 is unknown");
eq(guardAlertHtml({m2: {guard_ok: true}}), "", "healthy guard is quiet");
eq(guardAlertHtml({m2: {guard_ok: false}}), "중단 사유 미기록", "known stop without reason stays neutral");
eq(guardAlertHtml({m2: {guard_ok: false, guard_reason: "quota exhausted"}}),
  "quota exhausted", "real reason passes through raw for esc() at use site");
// glanceOpenKeys/restoreGlanceOpen: quiet poll keeps the open fold.
// Identity is stable data-repo (r.name), never the h2 badge text:
// a 주의→보통 verdict change must not lose the open fold.
function fakeGDet(key, open) { return {dataset: {gk: key}, open: open}; }
const gNeed = fakeGDet("need", true), gBots = fakeGDet("bots", false);
const gcard = {
  dataset: {repo: "synth-repo"},
  querySelectorAll: (s) => s === "details[data-gk][open]"
    ? [gNeed, gBots].filter((d) => d.open) : [gNeed, gBots]
};
const gbox = {querySelectorAll: (s) => s === ".grepo" ? [gcard] : []};
eq(JSON.stringify(glanceOpenKeys(gbox)), '["synth-repo||need"]',
  "captures only open folds with repo identity");
eq(JSON.stringify(glanceOpenKeys({querySelectorAll: () => []})), "[]",
  "no cards means no keys");
eq(JSON.stringify(glanceOpenKeys(null)), "[]", "null box is safe");
const gNeed2 = fakeGDet("need", false), gBots2 = fakeGDet("bots", false);
const gcard2 = {
  dataset: {repo: "synth-repo"},
  querySelectorAll: () => [gNeed2, gBots2]
};
const gbox2 = {querySelectorAll: (s) => s === ".grepo" ? [gcard2] : []};
eq(restoreGlanceOpen(gbox2, ["synth-repo||need"]), 1,
  "reopens the same-repo fold");
ok(gNeed2.open === true, "fold actually opened");
ok(gBots2.open === false, "other folds untouched");
eq(restoreGlanceOpen(gbox2, ["other-repo||need"]), 0,
  "foreign repo keys never restore");
eq(restoreGlanceOpen(gbox2, "nope"), 0, "non-array keys ignored");
// pendingChip: unknown stays count-free, never invents numbers.
eq(pendingChip({}), "", "no origin means no chip");
eq(pendingChip({origin_m2: "x", root_pending: false}), "자동 등록",
  "explicit false shows plain registration");
eq(pendingChip({origin_m2: "x"}), "자동 등록 · Root 확인 대기",
  "unknown count shows no invented number");
eq(pendingChip({origin_m2: "x", root_pending_count: 3}),
  "자동 등록 · Root 확인 대기 3건", "count 3 renders");
eq(pendingChip({origin_m2: "x", root_pending_count: 4}),
  "자동 등록 · Root 확인 대기 4건", "count 4 renders");
eq(pendingChip({origin_m2: "x", root_pending_count: {}}),
  "자동 등록 · Root 확인 대기", "malformed count stays count-free");
eq(pendingChip({origin_m2: "x", root_pending_count: "3<script>"}),
  "자동 등록 · Root 확인 대기", "hostile count stays count-free");
eq(esc(pendingChip({origin_m2: "x", root_pending_count: 3})),
  "자동 등록 · Root 확인 대기 3건", "composed chip escapes cleanly");
// applyThemeLabel: real branches against a stub theme button.
document.documentElement.dataset = {};
applyThemeLabel();
eq(__fakeBtn.textContent, "다크", "light default offers dark");
eq(__fakeBtn.attrs["aria-pressed"], "false", "light default not pressed");
document.documentElement.dataset = {theme: "dark"};
applyThemeLabel();
eq(__fakeBtn.textContent, "라이트", "dark offers light");
eq(__fakeBtn.attrs["aria-pressed"], "true", "dark pressed");
console.log("PASS " + pass);
'''


RENDER_FUNCTIONS = ["glanceRepoKey", "glanceOpenKeys", "glanceScrollTop",
                     "restoreGlanceScroll", "scrollerOf", "parseHash",
                     "restoreGlanceOpen", "renderGlanceView",
                     "bandAskId", "glanceBandVerdict", "glanceBandSentence",
                     "glanceBandStateKo", "glanceBandHtml", "glanceBandLoadingHtml", "bandLoadState",
                     "appHealthWarning", "paintGlanceBand", "fetchErrKo", "errBlockHtml", "subErrKo",
                     "errNewToBox"]

RENDER_DRIVER = r'''
const fs = require("fs");
const html = fs.readFileSync(process.argv[1], "utf8");
const m = html.match(/<script>([\s\S]*)<\/script>/);
if (!m) { console.log("FAIL no inline script"); process.exit(1); }
const src = m[1];
const wantFn = process.argv[2].split(",");
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
const escSrc = (src.match(/\nfunction esc\(s\) \{[^\n]*\}/) || [])[0] || "";
if (!escSrc) throw new Error("missing function esc");
function extractConstLine(name) {
  const re = new RegExp("(?:^|\\n)const\\s+" + name + "\\s*=([^;\\n]*;?)");
  const mm = re.exec(src);
  if (!mm) throw new Error("missing const " + name);
  return mm[0].trim();
}
const wantConst = ["M1_VERDICT_KO", "M1_VERDICT_CLS", "BAND_STALE_TOL_MS",
  "LOGIN_NEEDED_KO", "APIKEY_SET_KO", "SUB_APIKEY_RE", "SUB_LOGIN_RE"];
function extractConstBlock(name) {
  const re = new RegExp("(?:^|\\n)const\\s+" + name + "\\s*=\\s*\\{");
  const mm = re.exec(src);
  if (!mm) throw new Error("missing const block " + name);
  const brace = src.indexOf("{", mm.index);
  return "const " + name + " = " + src.slice(brace, sliceBalanced(src, brace)) + ";";
}
const wantBlock = ["FETCH_ERR_TEXT"];
const pieces = [escSrc.trim()];
for (const n of wantConst) pieces.push(extractConstLine(n));
for (const n of wantBlock) pieces.push(extractConstBlock(n));
for (const n of wantFn) { if (n !== "esc") pieces.push(extractFn(n)); }
for (const p of pieces) {
  if (/setInterval|setTimeout|addEventListener|localStorage/.test(p))
    throw new Error("extraction swallowed page boot code");
}
// Minimal harness: route/location/deferred-fetch stubs plus tiny leaf
// doubles for card markup only. Capture, sequence and route-guard logic
// under test is the actual production code, never reimplemented here.
const location = {hash: "#/glance"};
let __summaryImpl = null;
function jget(url) {
  if (__summaryImpl) return __summaryImpl(url);
  throw new Error("unexpected jget " + url);
}
let __viewBox = null;
function $(id) {
  if (id === "view") return __viewBox;
  if (id === "bartitle") return {textContent: ""};
  return null;
}
const document = {documentElement: {dataset: {}}, scrollingElement: null,
  activeElement: null};
const glanceSortRows = (rs) => (Array.isArray(rs) ? rs : []);
const glanceRepoCard = (r) => '<div class="card grepo">stub</div>';
const glossaryDetails = () => "";
const ago = (s) => String(s || "");
const focusViewHeading = () => false;
eval(pieces.join("\n"));
''' + r'''
let pass = 0;
function ok(c, label) { if (!c) { console.log("FAIL " + label); process.exit(1); } pass++; }
function eq(a, b, label) { if (a !== b) { console.log("FAIL " + label + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exit(1); } pass++; }
function makeDet(gk, open) { return {dataset: {gk: gk}, open: !!open}; }
function makeCard(repo, openList) {
  const dets = ["need", "blocked", "next", "bots", "story", "ledger"]
    .map((gk) => makeDet(gk, (openList || []).indexOf(gk) >= 0));
  return {
    dataset: {repo: repo},
    querySelectorAll: (s) => {
      if (s === "details[data-gk][open]") return dets.filter((d) => d.open);
      if (s === "details[data-gk]") return dets;
      return [];
    },
    _dets: dets
  };
}
let paints = [];
let liveCards = [];
function resetGlance() { paints = []; liveCards = []; }
const __box = {
  parentElement: null,
  attrs: {},
  setAttribute(k, v) { this.attrs[String(k)] = String(v); },
  removeAttribute(k) { delete this.attrs[String(k)]; },
  getAttribute(k) { const kk = String(k); return kk in this.attrs ? this.attrs[kk] : null; },
  querySelector: (s) => s === ".grepo" ? (liveCards[0] || null) : null,
  querySelectorAll: (s) => s === ".grepo" ? liveCards.slice() : []
};
Object.defineProperty(__box, "innerHTML", {configurable: true,
  get: () => "",
  set: (v) => {
    // 실제 DOM처럼 어떤 innerHTML 대입이든 자식을 바꾼다: 뼈대·오류도
    // liveCards를 비워, 닦은 뒤 되살리지 못하는 회귀를 잡는다.
    // 조용한 폴링은 뼈대를 그리지 않으므로 그대로 통과한다.
    paints.push(v);
    liveCards = v.indexOf("grepo") >= 0 ? [makeCard("synth-repo", [])] : [];
  }});
__viewBox = __box;
const GSUM = {served_at: "t", at: "t", repos: [{name: "synth-repo"}]};
var O = {repos: [{name: "synth-repo", sched_enabled: false}]};
let waiters = [];
__summaryImpl = (url) => {
  if (url === "/api/summary")
    return new Promise((res, rej) => { waiters.push({res: res, rej: rej}); });
  return Promise.resolve(null);
};
async function tick(n) { for (let i = 0; i < (n || 10); i++) await Promise.resolve(); }
async function main() {
  location.hash = "#/glance";
  document.scrollingElement = {scrollTop: 0};
  // First load: skeleton first (captured before any wipe), then content.
  resetGlance();
  const p1 = renderGlanceView();
  eq(paints.length, 1, "entry paints skeleton synchronously before fetch");
  ok(paints[0].indexOf("불러오는") >= 0, "first paint is the loading state");
  eq(__box.attrs["aria-busy"], "true", "loading sets aria-busy");
  waiters[0].res(GSUM); await p1; await tick();
  ok(paints.some((h) => h.indexOf("grepo") >= 0), "success paints content");
  eq("aria-busy" in __box.attrs, false, "aria-busy removed after content");
  // Quiet poll: user-opened need+ledger and scroll survive the rerender.
  liveCards[0]._dets[0].open = true;
  liveCards[0]._dets[5].open = true;
  document.scrollingElement.scrollTop = 844;
  const n2 = paints.length;
  const p2 = renderGlanceView();
  document.scrollingElement.scrollTop = 0;
  waiters[1].res(GSUM); await p2; await tick();
  eq(paints.length, n2 + 1, "quiet poll repaints exactly once");
  const kept = liveCards[0]._dets;
  ok(kept[0].open && kept[5].open, "open need+ledger restored after poll");
  ok(!kept[1].open && !kept[2].open, "other folds stay closed");
  eq(document.scrollingElement.scrollTop, 844, "scroll restored after poll");
  // Overlap: a stale success resolving first must never paint.
  const p3 = renderGlanceView();
  const p4 = renderGlanceView();
  const n3 = paints.length;
  waiters[2].res(GSUM); await p3; await tick();
  eq(paints.length, n3, "stale success never paints");
  waiters[3].res(GSUM); await p4; await tick();
  eq(paints.length, n3 + 1, "latest success paints");
  // A stale error after a newer success paints nothing.
  const p5 = renderGlanceView();
  const p6 = renderGlanceView();
  const n5 = paints.length;
  waiters[5].res(GSUM); await p6; await tick();
  eq(paints.length, n5 + 1, "newer success paints");
  waiters[4].rej(new Error("late")); await p5; await tick();
  eq(paints.length, n5 + 1, "stale error never paints");
  // Route change: neither success nor error may touch another route.
  const p7 = renderGlanceView();
  location.hash = "#/command";
  waiters[6].res(GSUM); await p7; await tick();
  eq(paints.length, n5 + 1, "success after route change never paints");
  location.hash = "#/glance";
  const p8 = renderGlanceView();
  location.hash = "#/repo/x?tab=reviews";
  const n8 = paints.length;
  waiters[7].rej(new Error("boom")); await p8; await tick();
  eq(paints.length, n8, "error after route change never paints");
  location.hash = "#/glance";
  // First-load error: busy flag clears and the human message paints.
  resetGlance();
  const pe = renderGlanceView();
  eq(__box.attrs["aria-busy"], "true", "error-path load sets aria-busy");
  waiters[8].rej(new Error("down")); await pe; await tick();
  eq("aria-busy" in __box.attrs, false, "aria-busy removed after error");
  ok(paints.some((h) => h.indexOf("inline-err") >= 0), "error paints the human message");
}
main().then(() => console.log("PASS " + pass),
  (e) => { console.log("FAIL async " + ((e && e.stack) || e)); process.exit(1); });
'''


class DashboardShellTest(unittest.TestCase):
    def test_shell_helpers(self):
        proc = subprocess.run(
            ["node", "-e", DRIVER, str(HTML), ",".join(FUNCTIONS)],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(
            proc.returncode, 0,
            f"node driver failed (rc={proc.returncode}):\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}")
        self.assertRegex(proc.stdout.strip(), r"^PASS \d+$")

    def test_glance_render_guards(self):
        proc = subprocess.run(
            ["node", "-e", RENDER_DRIVER, str(HTML),
             ",".join(RENDER_FUNCTIONS)],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(
            proc.returncode, 0,
            f"node render driver failed (rc={proc.returncode}):\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}")
        self.assertRegex(proc.stdout.strip(), r"^PASS \d+$")


if __name__ == "__main__":
    unittest.main()
