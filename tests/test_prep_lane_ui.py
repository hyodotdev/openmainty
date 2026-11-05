"""Prep-batch lane scoping through the REAL rendered work section.

The shipped ``paintPrepSection`` (plus its lane helpers) is extracted
from ``src/openmainty/dashboard.html`` and executed with stub DOM +
canned ``/api/batches`` data, so the test verifies the actual rendered
select options, the default choice, and the disabled/blocked states —
not a copy of the logic. Backend ``batches.prepare_integration``
remains the authoritative gate; the UI only filters and defaults.
"""

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src" / "openmainty" / "dashboard.html"

FUNCTIONS = ["prepBatchLanes", "prepBaseFullyReady", "batchClosureReady",
             "broadestReady", "defaultBatch",
             "prepEligibleBatches", "prepPickText", "prepOptionLabel",
             "prepRegInfo", "fcIsolationLine",
             "recConflictedPaths", "recOriginalTouched",
             "recHasPreparedSource", "recFollowupAllowed",
             "recFastcheckTruth",
             "paintPrepSection", "paintPrepFollowups",
             "paintRecoverySection", "paintDerivedFixSection",
             "jointButtonVisible"]

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

// ---- stub DOM + network (rendering seam only; logic stays shipped) ----
// esc is an unchanged prod dependency whose regex literal defeats the
// balanced-brace extractor (same harness limit as revision 50); it is
// stubbed here while the full inline script still gets node --check and
// the lane/render assertions below run the REAL paintPrepSection.
function esc(s) { return String(s == null ? "" : s); }
const GATE_KO = {pass: "통과", fail: "실패", blocked: "막힘", pending: "대기"};
let prepBatchCache = {}, prepBatchSel = {}, prepErr = {};
let prepFuDraft = {};
let __els = {}, __toasts = [], __urls = [], __batches = [];
let __calls = [], __renders = 0;
function $(id) { return __els[id] || null; }
function toast(t) { __toasts.push(t); }
async function jget(u) { __urls.push(u); return {batches: __batches}; }
async function jpost() { throw new Error("render test must not post"); }
function renderWorkView() { __renders++; }
function openBatchChanges(b, r, l, p) { __calls.push(["batch", b, r, l, p]); }
function pollJob() {}
function md(s) { return String(s == null ? "" : s); }
function mkSel() { return {innerHTML: "", value: "", title: "", disabled: false, onchange: null}; }
// Explicitly unregistered fixture: confident "none" keeps the prepare
// form enabled; a missing/unreadable QA must disable it (fail-closed).
const W_DONE = {integration_prep: null,
  details: {review: {status: "done"}, e2e: {status: "done"}},
  current_qa: {state: "none"}};
function opts() { return (__els["prep-batch"] || {}).innerHTML || ""; }

let pass = 0;
function eq(a, b, msg) {
  if (JSON.stringify(a) === JSON.stringify(b)) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function ok(c, msg) {
  if (c) { pass++; }
  else { console.log("FAIL " + msg); process.exitCode = 1; }
}
function whyText() { return (__els["prep-batch-why"] || {}).textContent || ""; }
function pickText() { return (__els["prep-batch-pick"] || {}).textContent || ""; }
async function render(lane, batches) {
  prepBatchCache = {}; prepBatchSel = {}; prepErr = {};
  __els = {"prep-batch": mkSel(), "prep-go": {disabled: false, onclick: null},
    "prep-batch-why": {textContent: ""}, "prep-batch-pick": {textContent: ""}};
  __toasts = []; __urls = []; __batches = batches;
  __calls = []; __renders = 0;
  const box = {innerHTML: ""};
  await paintPrepSection(box, W_DONE, {id: "run-x", repo: "r", lane: lane});
  for (let i = 0; i < 20 && !opts(); i++) await new Promise((r) => setTimeout(r, 10));
  return box;
}
async function renderW(w, s, batches) {
  prepBatchCache = {}; prepBatchSel = {}; prepErr = {};
  __els = {"prep-batch": mkSel(), "prep-go": {disabled: false, onclick: null},
    "prep-batch-why": {textContent: ""}, "prep-batch-pick": {textContent: ""},
    "prep-qa-batch": {onclick: null}, "prep-qa-refresh": {onclick: null},
    "prep-fu": {innerHTML: ""}};
  __toasts = []; __urls = []; __batches = batches;
  __calls = []; __renders = 0;
  const box = {innerHTML: ""};
  await paintPrepSection(box, w, s);
  // No-prep renders fill the select asynchronously; the prep-record
  // branch renders synchronously (its provenance marker is present
  // at once), so either condition ends the wait.
  for (let i = 0; i < 40 && !opts() && box.innerHTML.indexOf("출처·이력 보기") < 0; i++)
    await new Promise((r) => setTimeout(r, 10));
  return box;
}

const tests = `
;(async () => {
// Live-shaped batches, newest first like /api/batches ordering.
// Coherent repo (the real section groups by batch repo first).
const R = {repo: "r"};
const latestNext = Object.assign({id: "nitro-nitrogen0371-20261019", ready: 1, primaries: 1,
  purpose: "Nitrogen 0.37.1", attention: [], integrations: [{lane: "next"}]}, R);
const olderMain = Object.assign({id: "nitro-main-20261018", ready: 8, primaries: 8,
  purpose: "main stable", attention: [], integrations: [{lane: "main"}]}, R);
const expoNext = Object.assign({id: "nitro-expo58-20261018", ready: 1, primaries: 1,
  purpose: "Expo 58", attention: [], integrations: [{lane: "next"}]}, R);

// 1) main candidate: only the main batch rendered, defaulted to it.
// Hostile batch payloads (missing/string integrations) never anchor.
await render("main", [latestNext, olderMain, expoNext,
  {id: "bad-no-integrations"}, {id: "bad-string-integrations", integrations: "nope"}]);
eq(__els["prep-batch"].value, "nitro-main-20261018",
  "main defaults to main, not latest next");
// Purpose-first labels: human purpose leads, raw id stays the value.
ok(opts().indexOf(">main stable · main · 준비 8/8건<") >= 0,
  "main option label leads with purpose");
ok(opts().indexOf(">nitro-main-20261018 · 준비") < 0,
  "raw id no longer leads the label");
ok(opts().indexOf('value="nitro-main-20261018"') >= 0,
  "raw id stays the option value");
ok(pickText().indexOf("기준 — main stable · 레인 main · 준비 8/8건") >= 0,
  "chosen purpose wraps adjacent, not tooltip-only");
ok(opts().indexOf("nitro-nitrogen0371") < 0, "next batch hidden from main");
ok(opts().indexOf("nitro-expo58") < 0, "expo next hidden from main");
ok(opts().indexOf("bad-") < 0, "malformed batches hidden from main");
eq(__els["prep-go"].disabled, false, "main with main base stays enabled");

// 2) next candidate: main+next rendered, lane-aware default (first
// ready NEXT batch, not newest-first overall).
await render("next", [latestNext, olderMain, expoNext]);
eq(__els["prep-batch"].value, "nitro-nitrogen0371-20261019",
  "next defaults to first ready next batch");
ok(opts().indexOf("nitro-main-20261018") >= 0, "main stays for next");
// Changing the select refreshes the adjacent full-purpose line.
__els["prep-batch"].value = "nitro-main-20261018";
__els["prep-batch"].onchange();
ok(pickText().indexOf("기준 — main stable · 레인 main") >= 0,
  "pick line follows the changed selection");
// 2b) next->main continuity: main-only batches still anchor a next candidate.
await render("next", [olderMain]);
eq(__els["prep-batch"].value, "nitro-main-20261018", "next may anchor main");
eq(__els["prep-go"].disabled, false, "next on main stays enabled");
// 2c) lane-aware default: a newer ready MAIN batch never preselects for
// a next candidate when a ready NEXT batch exists (F3 regression).
await render("next", [olderMain, expoNext]);
eq(__els["prep-batch"].value, "nitro-expo58-20261018",
  "next skips newer ready main for first ready next");

// 3) unknown candidate lane: disabled with explicit visible reason, no default.
await render("", [latestNext, olderMain]);
eq(__els["prep-batch"].value, "", "unknown lane selects nothing");
eq(__els["prep-go"].disabled, true, "unknown lane disables prepare");
ok(opts().indexOf("기준 묶음 없음") >= 0, "unknown lane short placeholder rendered");
ok(whyText().indexOf("알 수 없어") >= 0, "unknown lane reason in visible text");

// 4) main candidate with next-only batches: disabled with lane reason.
await render("main", [latestNext, expoNext]);
eq(__els["prep-go"].disabled, true, "main on next-only disables prepare");
ok(whyText().indexOf("next") >= 0, "main-only-next reason in visible text");

// 5) lane matches but nothing fully ready: partial + attention excluded.
const partialMain = Object.assign({id: "nitro-main-20261018", ready: 3, primaries: 8,
  purpose: "main stable", attention: [], integrations: [{lane: "main"}]}, R);
await render("main", [partialMain]);
eq(__els["prep-go"].disabled, true, "partial 3/8 base disables prepare");
ok(whyText().indexOf("완전히 준비되지 않았습니다") >= 0, "partial reason in visible text");
const flaggedMain = Object.assign({id: "nitro-main-20261018", ready: 8, primaries: 8,
  purpose: "main stable", attention: [{key: "unit", label: "unit"}],
  integrations: [{lane: "main"}]}, R);
await render("main", [flaggedMain]);
eq(__els["prep-go"].disabled, true, "attention base disables prepare");
ok(whyText().indexOf("완전히 준비되지 않았습니다") >= 0, "attention reason in visible text");
// Eligible render leaves no stale reason text behind.
await render("main", [latestNext, olderMain]);
eq(whyText(), "", "eligible render clears the reason line");

// 6) registered-ready run (Bob shape): own batch excluded from
// targets, bound integration shown with inspect + refresh — the
// refresh only re-reads, so it is never labelled re-verify.
const devtools = Object.assign({id: "nitro-main-devtools-20261019", ready: 2, primaries: 2,
  purpose: "Prettier·Bob · main 개발 도구 검증", attention: [], integrations: [{lane: "main"}]}, R);
const main8 = Object.assign({id: "nitro-main-20261018", ready: 8, primaries: 8,
  purpose: "main 안정 8종 통합 검증", attention: [], integrations: [{lane: "main"}]}, R);
const WREG = {integration_prep: null,
  details: {review: {status: "done"}, e2e: {status: "done"}},
  current_qa: {state: "ready", ready: true, batch: "nitro-main-devtools-20261019",
    purpose: "Prettier·Bob · main 개발 도구 검증", lane: "main", reasons: [],
    gates_passed: 10, gates: []}};
const box6 = await renderW(WREG, {id: "run-bob", repo: "r", lane: "main"}, [devtools, main8]);
ok(box6.innerHTML.indexOf("이미 등록됨") >= 0, "registered ready shows bound panel");
ok(box6.innerHTML.indexOf("Prettier·Bob") >= 0, "registered panel names purpose");
ok(box6.innerHTML.indexOf("묶음에서 보기") >= 0, "registered panel offers inspect");
ok(box6.innerHTML.indexOf("최신 상태 다시 읽기") >= 0, "registered panel offers refresh");
ok(box6.innerHTML.indexOf("재검증") < 0, "refresh never labelled re-verify");
// An older eligible base must not invite a new prepare either: the
// whole selector/action hides for registered runs.
ok(box6.innerHTML.indexOf('id="prep-batch"') < 0, "no new-prepare selector for registered");
ok(box6.innerHTML.indexOf('id="prep-go"') < 0, "no new-prepare action for registered");
ok(box6.innerHTML.indexOf("nitro-main-20261018") < 0, "older base not offered either");
__els["prep-qa-batch"].onclick();
ok(__calls.length === 1 && __calls[0][1] === "nitro-main-devtools-20261019",
  "inspect opens the bound batch");
__els["prep-qa-refresh"].onclick();
eq(__renders, 1, "refresh re-reads the work view");

// 6b) only the own batch is lane-eligible: same hidden form, the
// registered panel itself is the duplicate explanation.
const box6b = await renderW(WREG, {id: "run-bob", repo: "r", lane: "main"}, [devtools]);
ok(box6b.innerHTML.indexOf('id="prep-go"') < 0, "sole own batch hides prepare");
ok(box6b.innerHTML.indexOf("이미 등록됨") >= 0, "panel explains instead of inviting");

// 6c) pure helper: exclusion returns the others, reason empty.
const sc6 = prepEligibleBatches([devtools, main8], "main", "nitro-main-devtools-20261019");
eq(sc6.eligible.map(b => b.id), ["nitro-main-20261018"], "exclusion returns others");
eq(sc6.reason, "", "exclusion with remaining targets needs no reason");

// 7) stale registration: exact reason shown, never green, own batch excluded.
const WSTALE = {integration_prep: null,
  details: {review: {status: "done"}, e2e: {status: "done"}},
  current_qa: {state: "stale", ready: false, batch: "nitro-main-devtools-20261019",
    purpose: "Prettier·Bob · main 개발 도구 검증", lane: "main",
    reasons: ["gate 'unit' inputs predate current integration source (STALE)"], gates: []}};
const box7 = await renderW(WSTALE, {id: "run-bob", repo: "r", lane: "main"}, [devtools, main8]);
ok(box7.innerHTML.indexOf("재검증 필요") >= 0, "stale shows reverify need");
ok(box7.innerHTML.indexOf("predate current integration source") >= 0, "exact stale reason shown");
ok(box7.innerHTML.indexOf("검증됨") < 0, "stale never greens");
ok(box7.innerHTML.indexOf('id="prep-go"') < 0, "stale hides new prepare too");
ok(box7.innerHTML.indexOf("묶음에서 보기") >= 0, "stale keeps inspect");
ok(box7.innerHTML.indexOf("최신 상태 다시 읽기") >= 0, "stale keeps refresh");

// 8) unreadable QA without batch: prepare disabled (fail-closed),
// exact reason + refresh path, no re-verify wording.
const WUNK = {integration_prep: null,
  details: {review: {status: "done"}, e2e: {status: "done"}},
  current_qa: {state: "unknown", reason: "묶음 목록을 읽지 못함"}};
const box8 = await renderW(WUNK, {id: "run-x", repo: "r", lane: "main"}, [devtools, main8]);
ok(box8.innerHTML.indexOf('id="prep-go"') < 0, "unknown QA hides prepare");
ok(box8.innerHTML.indexOf("현재 등록 상태를 읽지 못함") >= 0, "unreadable state shown");
ok(box8.innerHTML.indexOf("묶음 목록을 읽지 못함") >= 0, "exact unreadable reason shown");
ok(box8.innerHTML.indexOf("최신 상태 다시 읽기") >= 0, "refresh path offered");
ok(box8.innerHTML.indexOf("재검증") < 0, "no re-verify wording");

// 9) prep record + ready QA (Prettier shape): current integration
// first, the original record collapsed as history with gates intact.
const WPREP = {integration_prep: {status: "done", op_id: "prep-run-p-b",
    run_id: "run-p", batch_id: "b",
    result: {branch: "codex/review-x", head: "abc123def456"},
    gates: [{key: "source-prepared", status: "pass", note: "n"}],
    followups: [], updated_at: "t"},
  details: {review: {status: "done"}, e2e: {status: "done"}},
  current_qa: {state: "ready", ready: true, batch: "nitro-main-devtools-20261019",
    purpose: "Prettier·Bob · main 개발 도구 검증", lane: "main", reasons: [],
    gates_passed: 10, gates: []}};
const box9 = await renderW(WPREP, {id: "run-p", repo: "r", lane: "main"}, [devtools]);
ok(box9.innerHTML.indexOf("등록 묶음에서 검증됨") >= 0, "history banner authoritative");
ok(box9.innerHTML.indexOf("과거 준비 기록 보기") >= 0, "prep collapsed as history");
ok(box9.innerHTML.indexOf("source-prepared") >= 0, "original gates preserved");
ok(box9.innerHTML.indexOf("과거 준비 기록 보기") < box9.innerHTML.indexOf("다음 — 빠른 검사"),
  "old next-action lives inside history");

// 9b) prep record + stale QA: old next/native-pending instructions
// are explicitly then-record, originals intact, no pass relabelling.
const WPREPSTALE = {integration_prep: {status: "done", op_id: "prep-run-p-b",
    run_id: "run-p", batch_id: "b",
    result: {branch: "codex/review-x", head: "abc123def456"},
    gates: [{key: "native", status: "pending", note: "Root device QA after source freeze"}],
    followups: [], updated_at: "t"},
  details: {review: {status: "done"}, e2e: {status: "done"}},
  current_qa: {state: "stale", ready: false, batch: "nitro-main-devtools-20261019",
    purpose: "Prettier·Bob · main 개발 도구 검증", lane: "main",
    reasons: ["inherited batch 'x' not fully proven (0/1 ready)"], gates: []}};
const box9b = await renderW(WPREPSTALE, {id: "run-p", repo: "r", lane: "main"}, [devtools]);
ok(box9b.innerHTML.indexOf("재검증 필요") >= 0, "stale prep banner states need");
ok(box9b.innerHTML.indexOf("not fully proven") >= 0, "stale prep banner keeps exact reason");
ok(box9b.innerHTML.indexOf("아래는 당시 기록") >= 0, "stale prep marked then-record");
ok(box9b.innerHTML.indexOf('"native"') < 0, "no invented gate label");
ok(box9b.innerHTML.indexOf("native") >= 0, "original pending gate preserved");
ok(box9b.innerHTML.indexOf("아래는 당시 기록") < box9b.innerHTML.indexOf("다음 — 빠른 검사"),
  "stale next-action lives inside history");

// 9c) prep record + unknown QA: same then-record treatment.
const WPREPUNK = {integration_prep: {status: "done", op_id: "prep-run-p-b",
    run_id: "run-p", batch_id: "b",
    result: {branch: "codex/review-x", head: "abc123def456"},
    gates: [{key: "source-prepared", status: "pass", note: "n"}],
    followups: [], updated_at: "t"},
  details: {review: {status: "done"}, e2e: {status: "done"}},
  current_qa: {state: "unknown", reason: "현재 소스를 읽지 못함"}};
const box9c = await renderW(WPREPUNK, {id: "run-p", repo: "r", lane: "main"}, [devtools]);
ok(box9c.innerHTML.indexOf("상태 미확인") >= 0, "unknown prep banner states unknown");
ok(box9c.innerHTML.indexOf("현재 소스를 읽지 못함") >= 0, "unknown prep keeps exact reason");
ok(box9c.innerHTML.indexOf("아래는 당시 기록") >= 0, "unknown prep marked then-record");
ok(box9c.innerHTML.indexOf("source-prepared") >= 0, "unknown prep gates intact");

// 10) prep record + confidently unregistered QA: record renders as-is.
const WPREP0 = {integration_prep: {status: "done", op_id: "prep-run-p-b",
    run_id: "run-p", batch_id: "b",
    result: {branch: "codex/review-x", head: "abc123def456"},
    gates: [{key: "source-prepared", status: "pass", note: "n"}],
    followups: [], updated_at: "t"},
  details: {review: {status: "done"}, e2e: {status: "done"}},
  current_qa: {state: "none"}};
const box10 = await renderW(WPREP0, {id: "run-p", repo: "r", lane: "main"}, [devtools]);
ok(box10.innerHTML.indexOf("과거 준비 기록") < 0, "unregistered prep not historified");
ok(box10.innerHTML.indexOf("source-prepared") >= 0, "unregistered gates shown");

// 11) done typed issue prepare with executed steps offers registration.
// Visibility comes from the REAL shipped prepRegInfo and the isolation
// line from the REAL shipped fcIsolationLine; the server stays
// authoritative and must revalidate op/result/receipt before any write.
const WREGDONE = {integration_prep: {status: "done", op: "prepare",
  op_id: "prep-run-i-b", run_id: "run-i", batch_id: "b",
  issue: {issue: "856", repo: "r", lane: "next",
    url: "https://github.com/example/r/issues/856"},
  result: {branch: "codex/review-issue-856-abc123", head: "abc123def456"},
  fastcheck_summary: {ok: true, steps: [{gate: "unit", network_sandbox: "strict"}]},
  gates: [{key: "source-prepared", status: "pass", note: "n"}],
  followups: [], updated_at: "t"},
  details: {review: {status: "done"}, e2e: {status: "done"}},
  current_qa: {state: "none"}};
const box11 = await renderW(WREGDONE, {id: "run-i", repo: "r", lane: "next"}, []);
ok(box11.innerHTML.indexOf('id="prep-reg-go"') >= 0, "done issue prepare with steps offers registration");
ok(box11.innerHTML.indexOf("묶음으로 등록") >= 0, "register button labelled");
ok(box11.innerHTML.indexOf("빠른 검사 1단계 통과") >= 0, "real isolation line rendered from shipped helper");
ok(box11.innerHTML.indexOf("unit: 네트워크 차단") >= 0, "isolation line names the executed gate");

// 11b) dependency (non-issue) done prepare with steps: no registration,
// while its isolation line still renders from the same shipped helper.
const WDEP = JSON.parse(JSON.stringify(WREGDONE));
delete WDEP.integration_prep.issue;
WDEP.integration_prep.op_id = "prep-run-d-b";
WDEP.integration_prep.result = {branch: "codex/next/dep-bar", head: "abc123def456"};
const box11b = await renderW(WDEP, {id: "run-d", repo: "r", lane: "next"}, []);
ok(box11b.innerHTML.indexOf('id="prep-reg-go"') < 0, "dependency prepare never offers issue registration");
ok(box11b.innerHTML.indexOf("빠른 검사 1단계 통과") >= 0, "dependency steps still render their isolation line");

// 11c) empty steps: no registration, no isolation line.
const WEMPTY = JSON.parse(JSON.stringify(WREGDONE));
WEMPTY.integration_prep.fastcheck_summary = {ok: true, steps: []};
const box11c = await renderW(WEMPTY, {id: "run-i", repo: "r", lane: "next"}, []);
ok(box11c.innerHTML.indexOf('id="prep-reg-go"') < 0, "empty steps offer no registration");
ok(!/빠른 검사 \d+단계 (통과|실패)/.test(box11c.innerHTML), "empty summary renders no isolation line");

// 11d) malformed steps (non-array): no registration, no isolation line.
const WMAL = JSON.parse(JSON.stringify(WREGDONE));
WMAL.integration_prep.fastcheck_summary = {ok: true, steps: "unit"};
const box11d = await renderW(WMAL, {id: "run-i", repo: "r", lane: "next"}, []);
ok(box11d.innerHTML.indexOf('id="prep-reg-go"') < 0, "malformed steps offer no registration");
ok(!/빠른 검사 \d+단계 (통과|실패)/.test(box11d.innerHTML), "malformed summary renders no isolation line");

// 11e) missing op: no registration even with issue + executed steps.
const WNOOP = JSON.parse(JSON.stringify(WREGDONE));
delete WNOOP.integration_prep.op_id;
const box11e = await renderW(WNOOP, {id: "run-i", repo: "r", lane: "next"}, []);
ok(box11e.innerHTML.indexOf('id="prep-reg-go"') < 0, "missing op offers no registration");

// 11f) blocked issue prepare with steps: no registration, reason stays.
const WBLOCK = JSON.parse(JSON.stringify(WREGDONE));
WBLOCK.integration_prep.status = "blocked";
WBLOCK.integration_prep.error = "replay failed at commit 2";
const box11f = await renderW(WBLOCK, {id: "run-i", repo: "r", lane: "next"}, []);
ok(box11f.innerHTML.indexOf('id="prep-reg-go"') < 0, "blocked prepare offers no registration");
ok(box11f.innerHTML.indexOf("막힘 사유") >= 0, "blocked reason stays visible");
})();
`;
'''

DRIVER += 'Promise.resolve(eval(wantFn.map(extractFn).join("\\n") + "\\n" + tests))'
DRIVER += '.then(() => console.log("PASS " + pass),'
DRIVER += ' (e) => { console.log("FAIL driver " + (e && e.stack || e)); process.exitCode = 1; });\n'


class PrepLaneUiTests(unittest.TestCase):
    def run_driver(self):
        proc = subprocess.run(
            ["node", "-e", DRIVER, str(HTML), ",".join(FUNCTIONS)],
            capture_output=True, text=True, timeout=120)
        return proc

    def test_rendered_prep_section_scopes_bases_by_lane(self):
        proc = self.run_driver()
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout[-2000:]}"
                         f"{proc.stderr[-2000:]}")
        self.assertNotIn("FAIL", proc.stdout)
        self.assertIn("PASS", proc.stdout)

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
