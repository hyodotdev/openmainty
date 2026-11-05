"""Watch-inputs editor UI logic through the REAL shipped helpers.

The dashboard's pure watch helpers plus the impure ``paintWatchEditor``
are extracted from ``src/openmainty/dashboard.html`` and executed with
node against a stub DOM + canned ``/api/sched`` rows, so the test
verifies actual candidate derivation (root/example dedupe, unmanaged/
sdk/non-npm-pub exclusion, logical explicit/plain exclusion), error
mapping (lookup failure is an error, not stale), disclosure/focus
preservation across re-renders, save freezing, and stale handling —
not a copy of the logic.
"""

import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src" / "openmainty" / "dashboard.html"

FUNCTIONS = [
    "watchKeyIdent",
    "watchPeerChoices",
    "watchEditStatusCopy",
    "watchEditErrorKind",
    "watchEditNextState",
    "watchEditAdoptFresh",
    "watchEditCssEsc",
    "watchFilterChoices",
    "watchStagedSummary",
    "watchHistWhen",
    "watchScanSummary",
    "watchHistoryHtml",
    "paintWatchEditor",
    "rerenderWatchEditor",
]

STUBS = r'''
// ---- stub DOM + network (rendering seam only; logic stays shipped) ----
function esc(s) { return String(s == null ? "" : s); }
let schedCache = {};
let view = {kind: "work"};
let __loads = 0;
function loadAll() { __loads++; return Promise.resolve(); }
let __rows = [];
let __urls = [];
async function jget(u) {
  __urls.push(u);
  if (u.indexOf("/api/work?run=") === 0) {
    if (__workResp === "FAIL") throw new Error("work down");
    return __workResp;
  }
  if (__rows === "FAIL") throw new Error("sched down");
  return {last_rows: __rows, queue: []};
}
let __fetchBody = null;
let __fetchUrl = null;
let __fetchGate = null;
let __scanResp = null;
async function fetch(url, opts) {
  __fetchUrl = url;
  __fetchBody = JSON.parse(opts.body);
  if (url === "/api/sched/scan" && __scanResp) return __scanResp;
  if (__fetchGate) return __fetchGate;
  return {ok: false, status: 500, json: async () => ({error: "no stub response"})};
}
let __workResp = null;
let __activeEl = null;
const document = {
  get activeElement() { return __activeEl; },
  set activeElement(v) { __activeEl = v; }
};
function mkCtl(box, kind, key, checked, disabled) {
  return {key, dataset: kind === "cb" ? {weadd: key} : {werm: key},
    checked: !!checked, disabled: !!disabled, onclick: null, onchange: null,
    focus() { box.focusedCtl = this; }};
}
function mkSearch(box, value, disabled) {
  return {id: "watch-edit-q", dataset: {}, value: String(value == null ? "" : value),
    disabled: !!disabled, oninput: null, selectionStart: 0, selectionEnd: 0,
    focus() { box.focusedCtl = this; },
    setSelectionRange(a, b) { box.selRange = [a, b]; }};
}
function parseInto(box, htmlText) {
  box.cbs = [];
  box.rms = [];
  box.save = null; box.clear = false; box.reread = false; box.statusText = "";
  box.q = null; box.scanBtn = null; box.selRange = null;
  let m;
  const cbRe = /<input type="checkbox" data-weadd="([^"]+)"([^>]*)>/g;
  while ((m = cbRe.exec(htmlText)))
    box.cbs.push(mkCtl(box, "cb", m[1], /checked/.test(m[2]), /disabled/.test(m[2])));
  const rmRe = /<button class="btn sm" data-werm="([^"]+)"([^>]*)>/g;
  while ((m = rmRe.exec(htmlText)))
    box.rms.push(mkCtl(box, "rm", m[1], false, /disabled/.test(m[2])));
  const svRe = /<button class="btn sm hot" id="watch-edit-save"([^>]*)>([^<]*)<\/button>/;
  const sv = svRe.exec(htmlText);
  if (sv) box.save = {disabled: /disabled/.test(sv[1]), label: sv[2],
    onclick: null};
  box.clear = htmlText.indexOf('id="watch-edit-clear"') >= 0;
  box.reread = htmlText.indexOf('id="watch-edit-reread"') >= 0;
  const qRe = /<input type="search" id="watch-edit-q"([^>]*)>/;
  const qm = qRe.exec(htmlText);
  if (qm) {
    const vm = /value="([^"]*)"/.exec(qm[1]);
    box.q = mkSearch(box, vm ? vm[1] : "", /disabled/.test(qm[1]));
  }
  const scRe = /<button class="btn sm" id="watch-edit-scan"([^>]*)>([^<]*)<\/button>/;
  const sc = scRe.exec(htmlText);
  if (sc) box.scanBtn = {disabled: /disabled/.test(sc[1]), label: sc[2],
    onclick: null};
  // Several status lines can coexist (save feedback plus scan
  // feedback): the latest one carries the freshest state.
  const stRe = /<div class="ev" role="status">([\s\S]*?)<\/div>/g;
  let st = null, sm;
  while ((sm = stRe.exec(htmlText))) st = sm;
  box.statusText = st ? st[1] : "";
}
function mkBox() {
  const box = {_html: "", det: {open: false}, hist: {open: false},
    cbs: [], rms: [],
    save: null, clear: false, reread: false, statusText: "",
    q: null, scanBtn: null, selRange: null,
    focusedCtl: null, saveCtl: null, clearCtl: null, rereadCtl: null,
    scanCtl: null,
    contains(el) {
      return el === this.det || el === this.hist || el === this.q ||
        this.cbs.includes(el) || this.rms.includes(el);
    },
    querySelector(sel) {
      if (sel === "details") return this.det;
      if (sel === "#watch-edit-cand") return this.det;
      if (sel === "#watch-edit-hist") return this.hist;
      if (sel === "#watch-edit-q") return this.q;
      let m = /^\[data-weadd="([^"]+)"\]$/.exec(sel);
      if (m) return this.cbs.find(c => c.key === m[1]) || null;
      m = /^\[data-werm="([^"]+)"\]$/.exec(sel);
      if (m) return this.rms.find(c => c.key === m[1]) || null;
      return null;
    },
    querySelectorAll(sel) {
      if (sel === "[data-weadd]") return this.cbs;
      if (sel === "[data-werm]") return this.rms;
      return [];
    }};
  Object.defineProperty(box, "innerHTML", {
    get() { return this._html; },
    set(v) {
      this._html = String(v);
      // Like a real DOM replacement, new elements start closed and
      // unfocused: disclosure/focus after a re-render must come from
      // the shipped restore path, never from a sticky stub.
      this.det.open = false;
      this.hist.open = false;
      this.focusedCtl = null;
      parseInto(this, this._html);
    }
  });
  return box;
}
let __box = null;
function $(id) {
  if (id === "watch-edit") return __box;
  if (!__box) return null;
  if (id === "watch-edit-save") {
    if (!__box.save) return null;
    return {get disabled() { return __box.save.disabled; },
      set onclick(f) { __box.saveCtl = f; },
      get onclick() { return __box.saveCtl; }};
  }
  if (id === "watch-edit-clear") {
    if (!__box.clear) return null;
    return {set onclick(f) { __box.clearCtl = f; },
      get onclick() { return __box.clearCtl; }};
  }
  if (id === "watch-edit-reread") {
    if (!__box.reread) return null;
    return {set onclick(f) { __box.rereadCtl = f; },
      get onclick() { return __box.rereadCtl; }};
  }
  if (id === "watch-edit-scan") {
    if (!__box.scanBtn) return null;
    return {get disabled() { return __box.scanBtn.disabled; },
      set onclick(f) { __box.scanCtl = f; },
      get onclick() { return __box.scanCtl; }};
  }
  return null;
}

let pass = 0;
function eq(a, b, msg) {
  if (JSON.stringify(a) === JSON.stringify(b)) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function ok(c, msg) {
  if (c) { pass++; }
  else { console.log("FAIL " + msg); process.exitCode = 1; }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
function count(hay, needle) { return hay.split(needle).length - 1; }
'''

TESTS = r'''
;(async () => {
// ---- pure: key identity mirrors the server split rule ----
eq(watchKeyIdent("alpha"), "alpha", "plain key ident");
eq(watchKeyIdent("npm:alpha"), "alpha", "explicit manager stripped");
eq(watchKeyIdent("@scope/pkg"), "@scope/pkg", "scoped plain kept");
eq(watchKeyIdent("foo:bar"), "", "unknown prefix never a plain ident");
eq(watchKeyIdent(""), "", "empty ident");
eq(watchKeyIdent(null), "", "null ident");

// ---- pure: peer choices dedupe + exclusions, no invented keys ----
const ROWS = [
  {name: "alpha", kind: "npm", installed: "1.0.0", latest: "1.0.0", scope: "dependencies"},
  {name: "beta", kind: "npm", installed: "0.87.0", latest: "0.88.0", scope: "dependencies"},
  {name: "beta", kind: "npm", installed: "0.87.0", latest: "0.88.0", scope: "peerDependencies"},
  {name: "gamma", kind: "pub", installed: "2.0.0", latest: "2.0.0", scope: "dependencies"},
  {name: "myself", kind: "npm", installed: "0.0.0-use.local", latest: null,
    update_kind: "unmanaged", scope: "dependencies"},
  {name: "tool", kind: "sdk", installed: "3.44.0", latest: null,
    update_kind: "sdk", scope: "dependencies"},
  {name: "mtool", kind: "maven", installed: "1.0", latest: "1.1", scope: "dependencies"},
];
const ch = watchPeerChoices(ROWS, ["alpha"]);
eq(ch.map(c => c.key), ["beta", "gamma"], "root/example dedupe, unmanaged/sdk/maven out");
const b = ch[0];
eq([b.manager, b.installed, b.latest], ["npm", "0.87.0", "0.88.0"],
  "manager + fullname + versions shown");
eq(b.scopes, ["dependencies", "peerDependencies"], "actual scopes grouped");
eq(watchPeerChoices(ROWS, ["alpha", "npm:beta"]).map(c => c.key), ["gamma"],
  "watched plain + explicit forms both excluded, nothing else");
ok(watchPeerChoices(ROWS, ["npm:beta"]).map(c => c.key).indexOf("beta") < 0,
  "explicit watched form excludes the logical peer");
eq(watchPeerChoices([{name: "q", kind: "npm"}], []).length, 1, "lean row offered");
eq(watchPeerChoices([{name: "q", kind: "npm"}], [])[0].installed, "?",
  "missing versions are unknown, never invented");

// ---- pure: status copy + error mapping ----
eq(watchEditStatusCopy("loading"), "감시 대상을 읽는 중…", "loading copy");
eq(watchEditStatusCopy("saving"), "기준 저장 중…", "saving copy");
ok(watchEditStatusCopy("saved", "감시 1건 → 2건").indexOf("감시 1건 → 2건") >= 0,
  "saved keeps server detail");
ok(watchEditStatusCopy("busy").indexOf("다시 저장") >= 0, "busy copy retries");
ok(watchEditStatusCopy("stale").indexOf("다시 읽기") >= 0, "stale copy re-reads");
ok(watchEditStatusCopy("error", "woof").indexOf("woof") >= 0,
  "error keeps exact server cause");
eq(watchEditStatusCopy("nope"), "", "unknown status empty");
eq(watchEditErrorKind(409, "registry lookup failed for beta; refusing without changing anything"),
  "error", "lookup failure is an error, never stale");
eq(watchEditErrorKind(409, "watch baseline changed since read; re-read the watch report and retry"),
  "stale", "real stale stays stale");
eq(watchEditErrorKind(409, "worker busy: another maintenance worker holds the global lock; run unchanged"),
  "busy", "real busy stays busy");
eq(watchEditErrorKind(409, "live execution owns it; run unchanged"), "busy", "live owner is busy");
eq(watchEditErrorKind(404, "unknown key: dep-x"), "stale", "gone key re-reads");
eq(watchEditErrorKind(409, "not an active deferred watch; refusing"), "stale", "inactive re-reads");
eq(watchEditErrorKind(409, "duplicate watched peer: x is already watched; refusing"),
  "error", "duplicate keeps draft as error");
eq(watchEditErrorKind(500, "boom"), "error", "unknown failure is error");

// ---- pure: state transitions never leak across runs ----
const P = {runId: "run-1", fp: "f1", settled: "s1", adds: ["beta"], removes: [],
  status: "idle", msg: "", savedFp: null};
ok(watchEditNextState(P, {runId: "run-1", fp: "f1", settled: "s1"}) === P,
  "same report keeps state by reference");
const st1 = watchEditNextState(P, {runId: "run-1", fp: "f2", settled: "s1"});
eq([st1.status, st1.fp, st1.adds], ["stale", "f1", ["beta"]],
  "same-run change marks stale, keeps old baseline + draft");
const st2 = watchEditNextState(
  {runId: "run-1", fp: "f1", settled: "s1", adds: [], removes: [],
    status: "idle", msg: "", savedFp: null},
  {runId: "run-1", fp: "f2", settled: "s1"});
eq([st2.status, st2.fp, st2.adds], ["idle", "f2", []],
  "same-run change without draft adopts fresh");
const st3 = watchEditNextState(
  {runId: "run-1", fp: "f1", settled: "s1", adds: ["beta"], removes: [],
    status: "saved", msg: "저장됨 — x", savedFp: "f9"},
  {runId: "run-2", fp: "f9", settled: "s1"});
eq([st3.status, st3.adds, st3.savedFp, st3.msg], ["idle", [], null, ""],
  "run change resets draft + saved even when the fingerprint matches");
const st4 = watchEditNextState(
  {runId: "run-1", fp: "f1", settled: "s1", adds: [], removes: [],
    status: "saved", msg: "저장됨 — x", savedFp: "f2"},
  {runId: "run-1", fp: "f2", settled: "s1"});
eq(st4.status, "saved", "confirmed new fingerprint keeps saved");
const ad = watchEditAdoptFresh(
  {runId: "run-1", fp: "f1", settled: "s1", adds: ["beta", "gamma"], removes: ["old"],
    status: "stale", msg: "", savedFp: null},
  {runId: "run-1", fp: "f2", settled: "s1"});
eq([ad.status, ad.fp, ad.adds, ad.removes], ["idle", "f2", ["beta", "gamma"], ["old"]],
  "explicit re-read adopts fresh, clears stale, keeps draft verbatim");

// ---- DOM: disclosure + focus survive multi-select ----
__rows = [
  {name: "alpha", kind: "npm", installed: "1.0.0", latest: "1.0.0", scope: "dependencies"},
  {name: "beta", kind: "npm", installed: "0.87.0", latest: "0.88.0", scope: "dependencies"},
  {name: "beta", kind: "npm", installed: "0.87.0", latest: "0.88.0", scope: "peerDependencies"},
  {name: "gamma", kind: "pub", installed: "2.0.0", latest: "2.0.0", scope: "dependencies"},
  {name: "myself", kind: "npm", latest: null, update_kind: "unmanaged", scope: "dependencies"},
];
__box = mkBox();
const CTX1 = {runId: "run-1", repo: "r", itemKey: "k", primaryDep: "alpha",
  watchRec: {settled_at: "s1", inputs: {registry: {alpha: "1.0.0"}}}, fp: "f1"};
await paintWatchEditor(__box, CTX1);
eq(count(__box.innerHTML, 'data-weadd="beta"'), 1, "rendered beta once (dedupe)");
ok(__box.innerHTML.indexOf("myself") < 0, "unmanaged self never offered");
ok(__box.innerHTML.indexOf("주요 대상(유지)") >= 0, "primary marked keep-only");
__box.det.open = true;
let cb = __box.querySelector('[data-weadd="beta"]');
__activeEl = cb;
cb.checked = true;
cb.onchange();
await sleep(10); await sleep(10);
eq(__box.det.open, true, "first selection keeps candidates open");
eq(__box.focusedCtl && __box.focusedCtl.dataset.weadd, "beta", "focus returns to beta");
eq(watchEdit.adds, ["beta"], "beta drafted");
let cg = __box.querySelector('[data-weadd="gamma"]');
__activeEl = cg;
cg.checked = true;
cg.onchange();
await sleep(10); await sleep(10);
eq(__box.det.open, true, "second selection keeps candidates open");
eq(__box.focusedCtl && __box.focusedCtl.dataset.weadd, "gamma", "focus returns to gamma");
eq(watchEdit.adds, ["beta", "gamma"], "multi-select accumulates");
ok(!__box.save.disabled, "draft enables save");
ok(__box.innerHTML.indexOf("작성 중") >= 0 &&
  __box.innerHTML.indexOf("추가 2건(beta, gamma)") >= 0,
  "staged names stay visible");
ok(__box.innerHTML.indexOf("추가 최대 4건") >= 0 &&
  __box.innerHTML.indexOf("지금 추가 2/4") >= 0,
  "bounds hint plus counter shown");
ok(__box.innerHTML.indexOf('id="watch-edit-save"') <
  __box.innerHTML.indexOf('<details id="watch-edit-cand"'),
  "save toolbar sits before the long list");

// ---- DOM: sequential search keeps focus + caret, narrows the pool ----
let qi = __box.querySelector("#watch-edit-q");
ok(!!qi, "search input rendered");
__box.det.open = true;
__activeEl = qi;
qi.value = "p"; qi.selectionStart = 1; qi.selectionEnd = 1; qi.oninput();
await sleep(10); await sleep(10);
const n1 = count(__box.innerHTML, "data-weadd=");
ok(n1 >= 1, "first char narrows the pool");
eq(__box.det.open, true, "filtering keeps disclosure");
eq(__box.focusedCtl && __box.focusedCtl.id, "watch-edit-q", "filtering keeps focus");
eq(__box.selRange, [1, 1], "typed caret range restored");
let qi2 = __box.querySelector("#watch-edit-q");
__activeEl = qi2;
qi2.value = "pu"; qi2.selectionStart = 2; qi2.selectionEnd = 2; qi2.oninput();
await sleep(10); await sleep(10);
const n2 = count(__box.innerHTML, "data-weadd=");
ok(n2 <= n1 && n2 >= 1, "second char narrows further");
eq(__box.focusedCtl && __box.focusedCtl.id, "watch-edit-q", "focus kept mid-word");
eq(__box.selRange, [2, 2], "caret follows sequential typing");
let qi3 = __box.querySelector("#watch-edit-q");
__activeEl = qi3;
// mid-string insert: caret sits between u and b, must not jump to end
qi3.value = "puXb"; qi3.selectionStart = 3; qi3.selectionEnd = 3; qi3.oninput();
await sleep(10); await sleep(10);
ok(__box.innerHTML.indexOf("검색에 맞는 후보가 없습니다") >= 0,
  "mid-string query filters honestly");
eq(__box.focusedCtl && __box.focusedCtl.id, "watch-edit-q", "focus kept on mid-string edit");
eq(__box.selRange, [3, 3], "mid-string caret restored, never forced to end");
let qi4 = __box.querySelector("#watch-edit-q");
__activeEl = qi4;
qi4.value = "pub"; qi4.selectionStart = 3; qi4.selectionEnd = 3; qi4.oninput();
await sleep(10); await sleep(10);
eq(count(__box.innerHTML, "data-weadd="), 1, "full query isolates one");
eq(__box.selRange, [3, 3], "caret to end");
// search text never leaks into the save payload
__activeEl = null;

// ---- DOM: saving freezes every control; busy keeps the draft ----
let gateResolve = null;
__fetchGate = new Promise((res) => { gateResolve = res; });
__fetchBody = null;
__box.saveCtl();
await sleep(10); await sleep(10);
ok(__box.cbs.every(c => c.disabled), "checkboxes frozen while saving");
ok(__box.save.disabled && __box.save.label === "저장 중…", "save shows saving");
gateResolve({ok: false, status: 409,
  json: async () => ({error: "worker busy: another maintenance worker holds the global lock; run unchanged"})});
await sleep(10); await sleep(10);
ok(__box.statusText.indexOf("다시 저장") >= 0, "busy copy shown, exact cause kept");
eq(watchEdit.adds, ["beta", "gamma"], "busy keeps the draft");
ok(!__box.save.disabled, "busy allows retry");
eq(__fetchBody.add, ["beta", "gamma"], "save posts adds without versions");
eq([__fetchBody.expected_fp, __fetchBody.expected_settled_at,
  __fetchBody.repo, __fetchBody.key], ["f1", "s1", "r", "k"],
  "save echoes report identity for CAS");
__fetchGate = null;

// ---- DOM: external change marks stale through a delivered poll ----
const CTX2 = {runId: "run-1", repo: "r", itemKey: "k", primaryDep: "alpha",
  watchRec: {settled_at: "s1", inputs: {registry: {alpha: "1.0.0", beta: "0.88.0"}}}, fp: "f2"};
await paintWatchEditor(__box, CTX2);
ok(__box.statusText.indexOf("바뀐 설정") >= 0, "stale shown after external change");
eq(watchEdit.adds, ["beta", "gamma"], "stale preserves the draft");
eq(watchEdit.fp, "f1", "stale keeps the old baseline reference");
ok(__box.save.disabled, "stale disables save until re-read");
ok(__box.reread, "stale offers explicit re-read");
ok(__box.innerHTML.indexOf("추가 1건(gamma)") >= 0,
  "already-watched draft excluded from the staged summary");

// ---- DOM: stale 409, then explicit re-read GETs the fresh report ----
// The closure ctx still holds the old fp: adoption must come from a
// fresh /api/work response, never from reusing ctx. The draft (gamma)
// stays applicable after the server adds a different peer (beta).
__box = mkBox();
const CTX7 = {runId: "run-7", repo: "r", itemKey: "k", primaryDep: "alpha",
  watchRec: {settled_at: "s1", inputs: {registry: {alpha: "1.0.0"}}}, fp: "g1"};
await paintWatchEditor(__box, CTX7);
let c7 = __box.querySelector('[data-weadd="gamma"]');
c7.checked = true; c7.onchange();
await sleep(10); await sleep(10);
eq(watchEdit.adds, ["gamma"], "run-7 draft staged");
let staleGateResolve = null;
__fetchGate = new Promise((res) => { staleGateResolve = res; });
__box.saveCtl();
await sleep(10); await sleep(10);
staleGateResolve({ok: false, status: 409,
  json: async () => ({error: "watch baseline changed since read; re-read the watch report and retry"})});
await sleep(10); await sleep(10);
ok(__box.statusText.indexOf("바뀐 설정") >= 0, "stale save marks re-read");
ok(__box.reread, "stale save offers explicit re-read");
__workResp = {run: "run-7", state: {id: "run-7"},
  outcome: {watch_inputs_fp: "g2",
    watch: {settled_at: "s2", active: true, check_error: null,
      last_check: "c9", next_check: "n9",
      inputs: {registry: {alpha: "1.0.0", beta: "0.88.0"}}},
    watch_history: {total: 1, entries: [{at: "t9", actor: "dashboard",
      added: ["beta"], removed: [],
      old: {alpha: "1.0.0"}, new: {alpha: "1.0.0", beta: "0.88.0"},
      observed: {beta: "0.88.0"}, repo: "r", key: "k", run_id: "run-7",
      source: {}}]}}};
__box.rereadCtl();
await sleep(10); await sleep(10); await sleep(10); await sleep(10);
eq(watchEdit.fp, "g2", "explicit re-read adopts fetched fp, not closure fp");
eq(watchEdit.adds, ["gamma"], "re-read keeps the still-applicable draft");
ok(__urls.some(u => u.indexOf("/api/work?run=run-7") === 0),
  "re-read GETs the current work report");
ok(!__box.save.disabled, "adopted draft can save again");
let renewGateResolve = null;
__fetchGate = new Promise((res) => { renewGateResolve = res; });
__box.saveCtl();
await sleep(10); await sleep(10);
renewGateResolve({ok: true, status: 200,
  json: async () => ({ok: true, old_keys: ["alpha", "beta"],
    new_keys: ["alpha", "beta", "gamma"], observed: {gamma: "2.0.0"},
    fingerprint: "g2n", settled_at: "s2"})});
await sleep(10); await sleep(10);
eq(__fetchBody.expected_fp, "g2", "resave posts the renewed CAS fp");
eq(__fetchBody.add, ["gamma"], "resave posts the applicable draft only");
ok(__box.statusText.indexOf("저장됨") >= 0, "resave succeeds after re-read");
__fetchGate = null;
__workResp = null;

// ---- DOM: readonly check-now posts the existing scan endpoint ----
function scanBody(over) {
  return Object.assign({repo: "r", checked: 68, warnings: 0, warning_notes: [],
    registry_errors: 0, drifted: 1, queued_added: 0, queued_existing: 1,
    issues_open: 0, issues: [], issues_error: null,
    last_scan: "2026-10-19T04:00:00+00:00",
    next_scan: "2026-10-19T04:30:00+00:00", error: null, scan_ok: true,
    sources: {}, rows: [],
    watches: {checked: 1, changed: [], failed: []}}, over || {});
}
__scanResp = {ok: true, status: 200, json: async () => scanBody()};
const loadsBefore = __loads;
__box.scanCtl();
await sleep(10); await sleep(10); await sleep(10);
eq(__fetchUrl, "/api/sched/scan", "scan uses the readonly scan endpoint");
eq(__fetchBody, {repo: "r"}, "scan posts the context repo only");
ok(__box.statusText.indexOf("확인 완료") >= 0, "scan result shown, not faked");
ok(__box.statusText.indexOf("변화 없음") >= 0, "scan reports no change honestly");
ok(__loads > loadsBefore, "scan refreshes the visible report");
__scanResp = {ok: false, status: 500, json: async () => ({error: "scan boom"})};
__box.scanCtl();
await sleep(10); await sleep(10);
ok(__box.statusText.indexOf("scan boom") >= 0, "scan failure keeps exact cause");
__scanResp = null;
// ---- DOM: HTTP 200 with a failed scan never claims success ----
__scanResp = {ok: true, status: 200,
  json: async () => scanBody({scan_ok: false, registry_errors: 2,
    watches: {checked: 0, changed: [], failed: ["watch boom"]}})};
__box.scanCtl();
await sleep(10); await sleep(10);
ok(__box.statusText.indexOf("확인 완료") < 0, "partial scan never claims success");
ok(__box.statusText.indexOf("변화 없음") < 0, "unobserved inputs never called unchanged");
ok(__box.statusText.indexOf("watch boom") >= 0, "watch failure surfaces its cause");
__scanResp = null;

// ---- pure: history render from an actual audit-shaped fixture ----
const HIST = {total: 2, entries: [
  {at: "2026-10-19T03:30:00+00:00", actor: "dashboard", repo: "r", key: "k",
    run_id: "run-1", added: ["beta"], removed: [],
    old: {alpha: "1.0.0"}, new: {alpha: "1.0.0", beta: "0.88.0"},
    observed: {beta: "0.88.0"},
    source: {workCommit: "c0ffee1234567890abcd", workTreeFingerprint: "fp0123456789abcdef"}},
  {at: "2026-10-19T04:00:00+00:00", actor: "dashboard", repo: "r", key: "k",
    run_id: "run-1", added: [], removed: ["gamma"],
    old: {alpha: "1.0.0", gamma: "2.0.0"}, new: {alpha: "1.0.0"},
    observed: {},
    source: {workCommit: "c0ffee1234567890abcd", workTreeFingerprint: "fp0123456789abcdef"}},
]};
const hh = watchHistoryHtml(HIST);
ok(hh.indexOf("dashboard") >= 0, "history names the actor");
ok(hh.indexOf("추가 beta") >= 0, "history names added peers");
ok(hh.indexOf("제외 gamma") >= 0, "history names removed peers");
ok(hh.indexOf("alpha 1.0.0, beta 0.88.0") >= 0, "history shows new pins");
ok(hh.indexOf("run-1") >= 0, "history folds run identity");
ok(hh.indexOf("c0ffee123456") >= 0, "history folds short source pins");
ok(hh.indexOf("검증 통과·업그레이드가 아님") >= 0, "history labelled config-only");
ok(hh.indexOf("interventions") < 0 && hh.indexOf("old_inputs") < 0,
  "no raw history leaks into the surface");
const he = watchHistoryHtml({total: 0, entries: []});
ok(he.indexOf("기록된 기준 변경이 없습니다") >= 0, "honest empty history state");
ok(he.indexOf("처음 기록된 기준") < 0, "empty claims no origin story");
ok(watchHistoryHtml(null).indexOf("기록된 기준 변경이 없습니다") >= 0,
  "null history is empty, never an error");
ok(hh.indexOf("최근 변경") >= 0 && hh.indexOf("총 2건") >= 0,
  "latest summary line plus count");
ok(hh.indexOf('<details id="watch-edit-hist">') >= 0 &&
  hh.indexOf("<summary>최근 변경") >= 0,
  "history folds into one collapsed disclosure with the latest summary");
eq(count(hh, "<details"), 2, "history is one disclosure plus nested source, never flat rows");
eq(count(hh, "제외 gamma"), 1, "latest entry drawn once, never summary plus row");
eq(count(hh, "추가 beta"), 1, "older entry drawn once inside");
ok(hh.indexOf("<summary>실행·원본 식별 보기</summary>") >= 0,
  "source identity nests inside the history disclosure");
ok(he.indexOf("<details") < 0, "empty history stays flat truthful text");
// ---- pure: search filter + staged summary + history clock ----
const FC = [{key: "beta", manager: "npm", name: "beta", installed: "1", latest: "2", scopes: []},
  {key: "gamma", manager: "pub", name: "gamma", installed: "1", latest: "1", scopes: []}];
eq(watchFilterChoices(FC, "").length, 2, "empty query returns the pool");
eq(watchFilterChoices(FC, null).length, 2, "null query returns the pool");
eq(watchFilterChoices(FC, "pub").map(c => c.key), ["gamma"], "manager filter");
eq(watchFilterChoices(FC, "BETA").map(c => c.key), ["beta"], "case-insensitive filter");
eq(watchFilterChoices(FC, "zzz").length, 0, "no match is empty, never invented");
eq(watchStagedSummary([], []), "", "no draft means no summary");
ok(watchStagedSummary(["beta"], ["old"]).indexOf("추가 1건(beta)") >= 0,
  "staged names the adds");
ok(watchStagedSummary(["beta"], ["old"]).indexOf("제외 1건(old)") >= 0,
  "staged names the removes");
eq(watchHistWhen(""), "시각 기록 없음", "empty clock");
eq(watchHistWhen("2026-10-19T03:30:00+00:00"), "2026-10-19T03:30:00+00:00",
  "clock passthrough without formatter");
// ---- pure: scan verdict never fakes success ----
function fullScan(over) {
  return Object.assign({repo: "r", checked: 68, registry_errors: 0,
    issues_error: null, error: null, scan_ok: true,
    watches: {checked: 1, changed: [], failed: []}}, over || {});
}
let sv = watchScanSummary(fullScan());
eq(sv.ok, true, "clean scan succeeds");
ok(sv.msg.indexOf("변화 없음") >= 0, "clean scan reports no change");
sv = watchScanSummary(fullScan({watches: {checked: 1, changed: ["k"], failed: []}}));
ok(sv.ok && sv.msg.indexOf("변화 1건") >= 0, "changed scan names the lift");
sv = watchScanSummary(fullScan({scan_ok: false, registry_errors: 2}));
eq(sv.ok, false, "HTTP 200 partial scan is not success");
ok(sv.msg.indexOf("확인 완료") < 0, "partial scan hides the success copy");
sv = watchScanSummary(fullScan({watches: {checked: 0, changed: [],
  failed: ["watch boom"]}}));
eq(sv.ok, false, "watch failure fails the verdict");
ok(sv.msg.indexOf("watch boom") >= 0, "watch failure keeps its cause");
sv = watchScanSummary(fullScan({issues_error: "issues down"}));
eq(sv.ok, false, "issue failure fails the verdict");
sv = watchScanSummary({});
eq(sv.ok, false, "empty JSON is not a clean scan");
sv = watchScanSummary(null);
eq(sv.ok, false, "null body is not a clean scan");
sv = watchScanSummary(fullScan({scan_ok: undefined}));
eq(sv.ok, false, "missing scan_ok is not a clean scan");
sv = watchScanSummary(fullScan({scan_ok: "yes"}));
eq(sv.ok, false, "non-boolean scan_ok is not a clean scan");
const hm = watchHistoryHtml({total: 7, entries: HIST.entries});
ok(hm.indexOf("그 외 5건") >= 0, "older entries counted, never dumped");

// ---- DOM: history disclosure tracked apart from candidates ----
__box = mkBox();
const CTXH = {runId: "run-1", repo: "r", itemKey: "k", primaryDep: "alpha",
  watchRec: {settled_at: "s1", inputs: {registry: {alpha: "1.0.0"}}}, fp: "f1",
  history: {total: 1, entries: [{at: "t1", actor: "dashboard", repo: "r",
    key: "k", run_id: "run-1", added: ["beta"], removed: [],
    old: {alpha: "1.0.0"}, new: {alpha: "1.0.0", beta: "0.88.0"},
    observed: {}, source: {}}]}};
await paintWatchEditor(__box, CTXH);
ok(__box.innerHTML.indexOf('<details id="watch-edit-hist">') >= 0,
  "history disclosure rendered with its own identity");
__box.det.open = false;
__box.hist.open = true;
let hcb = __box.querySelector('[data-weadd="beta"]');
__activeEl = hcb;
hcb.checked = true;
hcb.onchange();
await sleep(10); await sleep(10);
eq(__box.hist.open, true, "rerender keeps the open history disclosure");
eq(__box.det.open, false, "closed candidates never reopen as history");
eq(__box.focusedCtl && __box.focusedCtl.dataset.weadd, "beta",
  "checkbox focus still restored beside history");

// ---- DOM: run change resets everything, saved never leaks ----
watchEdit = {runId: "run-1", fp: "f2", settled: "s1", adds: [], removes: [],
  status: "saved", msg: "저장됨 — x", savedFp: "f2", rows: __rows, rowsError: ""};
const CTX3 = {runId: "run-9", repo: "r", itemKey: "k9", primaryDep: "alpha",
  watchRec: {settled_at: "s1", inputs: {registry: {alpha: "1.0.0"}}}, fp: "f2"};
await paintWatchEditor(__box, CTX3);
ok(__box.statusText.indexOf("저장됨") < 0, "no saved-from-another-run shown");
eq([watchEdit.adds, watchEdit.status, watchEdit.savedFp], [[], "idle", null],
  "run change resets editor state");
})();
'''

EXTRACTOR = r'''
const fs = require("fs");
const html = fs.readFileSync(process.argv[1], "utf8");
const m = html.match(/<script>([\s\S]*)<\/script>/);
if (!m) { console.log("FAIL no inline script"); process.exit(1); }
const src = m[1];
const wantFn = process.argv[2].split(",");
const wantConst = ["WATCH_KEY_MANAGERS", "WATCH_PEER_MANAGERS"];

// Extraction seam: exact top-level declaration boundaries in this
// repository's style (column-0 closing lines), never a brace-counting
// lexer over regex literals. A wrong boundary fails node --check and
// execution loudly instead of silently slicing.
function extractBlock(startRe, endTest) {
  const lines = src.split("\n");
  let start = -1;
  for (let i = 0; i < lines.length; i++) {
    if (startRe.test(lines[i])) { start = i; break; }
  }
  if (start < 0) throw new Error("missing " + startRe);
  for (let i = start; i < lines.length; i++) {
    if (endTest(lines[i], i > start)) return lines.slice(start, i + 1).join("\n");
  }
  throw new Error("unterminated " + startRe);
}
function extractFn(name) {
  return extractBlock(new RegExp("^(async )?function " + name + "\\("),
    (l, past) => past && l === "}");
}
function extractConst(name) {
  return extractBlock(new RegExp("^const " + name + " = "),
    (l) => /;\s*$/.test(l));
}
function extractLet(name) {
  return extractBlock(new RegExp("^let " + name + " = "),
    (l) => /;\s*$/.test(l));
}
'''
RUNNER = r'''
Promise.resolve(eval(
  wantConst.map(extractConst).join("\n") + "\n" +
  extractLet("watchEdit") + "\n" +
  wantFn.map(extractFn).join("\n") + "\n" + __TESTS_JS))
.then(() => console.log("PASS " + pass),
 (e) => { console.log("FAIL driver " + (e && e.stack || e)); process.exitCode = 1; });
'''

DRIVER = (EXTRACTOR + STUBS + "\nconst __TESTS_JS = " + json.dumps(TESTS) +
          ";\n" + RUNNER)


class WatchInputsUiTests(unittest.TestCase):
    def run_driver(self):
        proc = subprocess.run(
            ["node", "-e", DRIVER, str(HTML), ",".join(FUNCTIONS)],
            capture_output=True, text=True, timeout=120)
        return proc

    def test_shipped_watch_editor_helpers_and_render(self):
        proc = self.run_driver()
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout[-2000:]}"
                         f"{proc.stderr[-2000:]}")
        self.assertNotIn("FAIL", proc.stdout)
        self.assertIn("PASS", proc.stdout)

    def test_extraction_boundaries_check(self):
        import re
        import tempfile
        html = HTML.read_text(encoding="utf-8")
        src = html[html.index("<script>") + len("<script>"):html.index("</script>")]
        lines = src.split("\n")

        def block(start_re, end_test):
            start = next(i for i, l in enumerate(lines)
                         if re.match(start_re, l))
            for i in range(start, len(lines)):
                if end_test(lines[i], i > start):
                    return lines[start:i + 1]
            raise AssertionError(f"unterminated {start_re}")

        chunks = []
        for name in FUNCTIONS:
            chunk = block(r"^(async )?function " + name + r"\(",
                          lambda l, past: past and l == "}")
            self.assertTrue(chunk[0].startswith("async function " + name)
                            or chunk[0].startswith("function " + name),
                            f"seam start for {name}")
            self.assertEqual(chunk[-1], "}", f"seam end for {name}")
            chunks.append("\n".join(chunk))
        for name in ("WATCH_KEY_MANAGERS", "WATCH_PEER_MANAGERS"):
            chunk = block(r"^const " + name + r" = ",
                          lambda l, _past: l.rstrip().endswith(";"))
            chunks.append("\n".join(chunk))
        chunk = block(r"^let watchEdit = ",
                      lambda l, _past: l.rstrip().endswith(";"))
        chunks.append("\n".join(chunk))
        with tempfile.NamedTemporaryFile(
                "w", suffix=".js", delete=False,
                encoding="utf-8") as fh:
            fh.write("\n".join(chunks))
            name = fh.name
        proc = subprocess.run(
            ["node", "--check", name],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0,
                         f"extracted seam failed node --check: "
                         f"{proc.stderr[-2000:]}")

    def test_dashboard_script_syntax(self):
        import tempfile
        html = HTML.read_text(encoding="utf-8")
        start = html.index("<script>")
        end = html.index("</script>")
        inline = html[start + len("<script>"):end]
        with tempfile.NamedTemporaryFile(
                "w", suffix=".js", delete=False,
                encoding="utf-8") as fh:
            fh.write(inline)
            name = fh.name
        proc = subprocess.run(
            ["node", "--check", name],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0,
                         f"node --check failed: {proc.stderr[-2000:]}")


if __name__ == "__main__":
    unittest.main()
