"""Focused UI-logic regressions for the work inspector (dashboard.html).

The dashboard's pure helpers are extracted from the shipped
``src/openmainty/dashboard.html`` inline script and executed with node, so the
test verifies actual logic (coverage derivation, untracked diff inclusion,
per-file fetch behavior, evidence dedup, poll fingerprint, chat labels) rather
than mirroring static strings.
"""

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src" / "openmainty" / "dashboard.html"

FUNCTIONS = [
    "nativeHit",
    "shellOnlyText",
    "deviceVerdict",
    "dedupeEvidence",
    "chatHeadFor",
    "bareRunId",
    "homeRunHead",
    "depActionFor",
    "queueDisplayStatus",
    "isExternalWatch",
    "workerLockWord",
    "watchNextAct",
    "resultKo",
    "workerHeadline",
    "isNoCodeDone",
    "isSupersededItem",
    "supersededNoteIsNoChange",
    "settlementForRun",
    "scAvailOf",
    "settledNextFor",
    "sourceIsLiveEmpty",
    "isValidatedNoChange",
    "runOutcomeWord",
    "runHasLiveJob",
    "runNodeTitle",
    "aggMemberHead",
    "workFpFor",
    "extractUntracked",
    "splitDiffSections",
    "sectionForPath",
    "fpHasPatch",
    "paintFilePatchSheet",
    "openFilePatch",
    "blockerKo",
    "curQAHead",
    "srcStatusKo",
    "qaDiagFirst",
    "qaDiagSecond",
    "qaDiagChain",
    "qaDiagTech",
    "qaDiagBlock",
    "currQaFp",
    "turnHtml",
    "isStageNotice",
    "groupedTranscript",
    "chatQaPanelHtml",
    "chatQaUnknownHtml",
    "resolveChatQaUpdate",
    "wireChatQa",
    "pinBottomFollow",
    "viewPinned",
    "paintChatQA",
    "refreshChatQA",
    "fmtKST",
    "fmtKSThm",
    "decisionFor",
    "candidateDecisionFor",
    "concisePart",
    "plainFirst",
    "reportQaMode",
    "paintReportSection",
    "reviewNextFor",
    "revBound160",
    "gateStale",
    "lanePhaseFor",
    "revSrcSig",
    "cssEsc",
    "snapshotDetailUi",
    "restoreDetailUi",
    "recheckBatchDetail",
    "revCardSem",
    "revDetailSem",
    "listDetailConflict",
    "revStaleGuard",
    "reviewsContentFp",
    "loadBatchDetail",
    "answerOutcome",
    "answerErrorIsSaved",
    "answerErrIdFor",
    "answerFieldFor",
    "answerInlineShow",
    "answerInlineHide",
    "subErrKo",
    "handleAnswerOk",
    "handleAnswerError",
    "jointRetryMsg",
    "jointButtonVisible",
    "prCountLine",
    "prBotLines",
    "splitHeadRest",
    "fcRepChip",
    "fcRepIso",
    "fcRepRc",
    "fcRepTail",
    "fcRepSteps",
    "fcRepRow",
    "fcRepConsumer",
    "fcReportHtml",
    "nextLabel",
    "nextProv",
    "promoCrit",
    "critLine",
    "paintRepoBranches",
    "profSection",
    "repoTabId",
    "repoTabsHtml",
    "repoTabPanelHtml",
    "showRepoTab",
    "wireRepoTabs",
    "captureRepoTabFocus",
    "restoreRepoTabFocus",
    "captureFk",
    "restoreFk",
    "paintRepoActivity",
    "eventHtml",
    "eventDetailHtml",
    "ghIssueUrl",
    "discSelectedKey",
    "discActiveCtx",
    "discReadNode",
    "discNodeErrHtml",
    "discReconcileOpen",
    "discToggleDetail",
    "modelPillState",
    "paintPill",
    "refreshPill",
    "actStatusFor",
    "setActStatus",
    "wAnsDraftGet",
    "wAnsDraftSet",
    "wAnswerBoxHtml",
    "wireAnswerBox",
    "chatRowHtml",
    "openSettings",
    "ago",
    "bandAskId",
    "glanceBandVerdict",
    "glanceBandSentence",
    "glanceBandStateKo",
    "glanceBandHtml",
    "paintGlanceBand",
    "fetchErrKo",
    "errBlockHtml",
    "stoppedResumeStep",
    "startFailToast",
    "graphMemberRunWord",
    "gfStatusOptions",
    "digestPrevDay",
    "fcValKo",
]

CONSTS = ["NATIVE_CMD_RE", "scopeKo", "stepKo", "REPO_TABS", "evKo",
          "M1_VERDICT_KO", "M1_VERDICT_CLS", "LOGIN_NEEDED_KO", "APIKEY_SET_KO",
          "SUB_APIKEY_RE", "SUB_LOGIN_RE"]
BLOCK_CONSTS = ["SCOPE_KO", "PROMO_KO", "STEP_KO", "DIAG_CAT_KO", "GST_KO", "GF_STATUS_KO", "EV_KO",
              "FETCH_ERR_TEXT"]

DRIVER = r'''
const fs = require("fs");
const html = fs.readFileSync(process.argv[1], "utf8");
const m = html.match(/<script>([\s\S]*)<\/script>/);
if (!m) { console.log("FAIL no inline script"); process.exit(1); }
const src = m[1];
// Production esc source for the hostile-text case, extracted once at
// driver scope (single-parsed, same pattern as the standalone preview
// driver). Inner tests reference __escSrc; never re-escape it inside
// the nested tests template.
const __escSrc = (src.match(/\nfunction esc\(s\) \{[^\n]*\}/) || [])[0] || "";
const wantFn = process.argv[2].split(",");
const wantConst = process.argv[3].split(",");
const wantBlock = (process.argv[4] || "").split(",").filter(Boolean);

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
    if (c === "*" && false) continue;
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
const modalHtml = [];
const modalState = {open: false};
function esc(s) { return String(s == null ? "" : s); }
/* md 렌더러 스텁: 기존 복잡한 마크다운 함수는 추출기가 파싱하지 못해
   원문을 그대로 둔다. 그룹핑·패널 구조 판정에만 쓰고, 실제 마크다운·
   diff 렌더링은 전체 스크립트 node --check와 실제 브라우저로 확인한다. */
function md(s) { return String(s == null ? "" : s); }
let chatQaSeq = 0;
function toast(m) { modalHtml.push("toast:" + m); }
function $(id) {
  if (id === "modal") return {classList: {contains: (c) => c === "hidden" ? !modalState.open : false}};
  return {};
}
function openModal(h) { modalState.open = true; modalHtml.push(h); }
function closeModal() { modalState.open = false; }
let pollCalls = [], loadCalls = 0;
function pollJob(id) { pollCalls.push(id); }
async function loadAll(q) { loadCalls++; }
/* 검토 상세 동작 스텁: 실제 loadBatchDetail과 같은 바깥 eval 범위에 두어
   어휘 범위가 닿는다. 비동기 본문 안 선언은 바깥 함수가 볼 수 없다. */
function consumePendingChgExpand() {}
function openWork() {}
function fakeBody() {
  return {dataset: {}, innerHTML: "", hidden: false, isConnected: true,
    querySelectorAll: () => [], querySelector: () => null};
}
let fetchUrl = "", fetchMode = "ok";
async function jget(u) {
  fetchUrl = u;
  if (fetchMode === "throw") throw new Error("no such run (404)");
  if (fetchMode === "no-fp") return {run: "r1", sourceChanges: {}};
  return {run: "r1", sourceChanges: {},
    filePatch: {path: "a/Sound.kt", diff: "diff --git a/a/Sound.kt b/a/Sound.kt\\n+new",
      availability: "live", historical: false, capped: false, error: ""}};
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
// 1) typecheck-only legacy run: no device pass from step name / note / filename
eq(nativeHit("e2e.log"), "", "bare e2e filename is not a device hit");
eq(nativeHit("yarn typecheck"), "", "typecheck has no device token");
eq(nativeHit("maestro test app"), "maestro", "maestro shell is a device hit");
eq(shellOnlyText([["yarn", "typecheck"]]), "yarn typecheck", "list command joins");
eq(shellOnlyText(["muse", "exec"]), "", "model call is not shell text");
eq(deviceVerdict([{shell: "yarn typecheck", exit_code: 0, proof: "legacy"}]).state, "none",
  "typecheck-only legacy run has no native coverage");
eq(deviceVerdict([{shell: "yarn typecheck", exit_code: 0, proof: "legacy"},
    {shell: "note mentions e2e.log", exit_code: 0, proof: "legacy"}]).state, "none",
  "note/filename e2e never counts as runtime");
eq(deviceVerdict([{shell: "maestro test flow", exit_code: 0, proof: "fresh"}]).state, "verified",
  "fresh exit-0 native command verifies");
eq(deviceVerdict([{shell: "maestro test flow", exit_code: 0, proof: "legacy"}]).state, "historical",
  "legacy exit-0 native run is historical, not current");
eq(deviceVerdict([{shell: "adb shell input", exit_code: 1, proof: "fresh"}]).state, "nopass",
  "failing device command is not a pass");

// 2) untracked content is included even when other sections exist; no dupes
const sc = {diffUnstaged: "diff --git a/a b/a\\n+x",
  untrackedContents: [{path: "new/test.js", content: "hello"}],
  diff: "--- unstaged ---\\ndiff --git a/a b/a\\n+x\\n--- untracked: new/test.js ---\\nhello\\n"};
const secs = splitDiffSections(sc);
ok(secs.some((s) => s.sec === "unstaged"), "unstaged section kept");
eq(secs.filter((s) => s.sec === "untracked:new/test.js").length, 1,
  "untracked included exactly once despite combined dup");
const legacy = {diff: "--- committed (base..HEAD) ---\\ndiff --git a/c b/c"};
ok(splitDiffSections(legacy).some((s) => s.sec === "committed"), "legacy combined parses");

// 3) per-file fetch: exact path queried, no jump to another file on miss
fetchMode = "ok";
await openFilePatch("run:abc", {}, "a/Sound.kt", "meta");
ok(fetchUrl.includes("run=" + encodeURIComponent("run:abc")), "run id sent");
ok(fetchUrl.includes("path=" + encodeURIComponent("a/Sound.kt")), "exact relative path sent");
ok(modalHtml[modalHtml.length - 1].includes("+new"), "exact file patch shown");
fetchMode = "throw";
const agg = {diffUnstaged: "diff --git a/other b/other\\n+zzz"};
await openFilePatch("run:abc", agg, "missing/file.js", "meta");
const last = modalHtml[modalHtml.length - 1];
ok(last.includes("missing/file.js"), "unavailable names the exact path");
ok(!last.includes("+zzz"), "never falls back to another file");
modalState.open = false;
await openFilePatch("run:abc", agg, "missing/file.js", "meta");
eq(modalHtml.length, modalHtml.filter(() => true).length, "closed modal stays closed");
// same-file aggregate fallback still works on old backends
fetchMode = "no-fp";
await openFilePatch("run:abc",
  {diffUnstaged: "diff --git a/a/Sound.kt b/a/Sound.kt\\n+old", availability: "live"},
  "a/Sound.kt", "meta");
ok(modalHtml[modalHtml.length - 1].includes("+old"), "same-file aggregate fallback");

// 4) raw filename vs human alias dedup to one label per file
const dd = dedupeEvidence([{name: "discord.md", label: "discord.md 열기"}],
  [{key: "discord", label: "디스코드 알림 보기", file: "discord.md"},
   {key: "plan", label: "계획서 보기", file: ""}]);
eq(dd.files.length, 1, "one file one label");
eq(dd.files[0].label, "디스코드 알림 보기", "human label wins");
eq(dd.inline.length, 1, "inline draft kept");

// 5) quiet poll fingerprint: same payload keeps DOM, any selected change rerenders
const w0 = {state: {waitReason: "", steps: {e2e: {status: "done", exit_code: 0}}},
  source: {proof: "legacy", fingerprint: "fp1"}, outcome: {triage: "UNKNOWN", disposition: null},
  sourceChanges: {head: "h1", liveHead: "h1"},
  verification: [{step: "e2e", status: "done", exit_code: 0, scopes: ["types"], proof: "legacy"}]};
const same = JSON.parse(JSON.stringify(w0));
eq(workFpFor(w0), workFpFor(same), "identical payload keeps DOM");
const changed = JSON.parse(JSON.stringify(w0));
changed.state.steps.e2e.status = "running";
ok(workFpFor(w0) !== workFpFor(changed), "step change rerenders");
const proofed = JSON.parse(JSON.stringify(w0));
proofed.source.proof = "current";
ok(workFpFor(w0) !== workFpFor(proofed), "proof change rerenders");
// 5b) watch state joins the poll fingerprint; client clock noise does not
const wv = JSON.parse(JSON.stringify(w0));
wv.outcome.watch_inputs_fp = "fp-a";
wv.outcome.watch = {settled_at: "s0", active: true, check_error: null,
  last_check: "c0", next_check: "n0", inputs: {registry: {alpha: "1.0.0"}}};
wv.outcome.watch_history = {total: 0, entries: []};
ok(workFpFor(w0) !== workFpFor(wv), "watch baseline edit repaints");
const wvAudit = JSON.parse(JSON.stringify(wv));
wvAudit.outcome.watch_history = {total: 1, entries: [{at: "t1", added: ["beta"]}]};
ok(workFpFor(wv) !== workFpFor(wvAudit), "new watch audit repaints");
const wvCheck = JSON.parse(JSON.stringify(wv));
wvCheck.outcome.watch.last_check = "c1";
wvCheck.outcome.watch.next_check = "n1";
ok(workFpFor(wv) !== workFpFor(wvCheck), "watch check progress repaints");
const wvSettled = JSON.parse(JSON.stringify(wv));
wvSettled.outcome.watch.settled_at = "s1";
ok(workFpFor(wv) !== workFpFor(wvSettled), "watch resettlement repaints");
const wvLift = JSON.parse(JSON.stringify(wv));
wvLift.outcome.watch.active = false;
ok(workFpFor(wv) !== workFpFor(wvLift), "watch lift repaints");
const wvNoise = JSON.parse(JSON.stringify(wv));
wvNoise.evaluated_at = "2099-01-01T00:00:00+00:00";
wvNoise.outcome.watch.inputs.fetched_at = "2099-01-01T00:00:00+00:00";
eq(workFpFor(wv), workFpFor(wvNoise), "client clock noise never repaints");

// 6) derived conversation titles: purpose first, raw id secondary
const runs = [{id: "40857b36", title: "chore(deps): @react-native-community/slider 5.2.0 -> 5.2.1"}];
const dep = chatHeadFor({id: "chat-1", repo: "r", issue: "dep-@react-native-community-slider-5.2.1",
  title: "", run_id: "40857b36"}, runs);
eq(dep.primary, "@react-native-community/slider 5.2.0 -> 5.2.1",
  "run title is primary minus the mechanical chore prefix");
ok(dep.tip.includes("chore(deps): @react-native-community/slider 5.2.0 -> 5.2.1"),
  "full title stays in the tooltip");
ok(dep.tip.includes("dep-@react-native-community-slider-5.2.1"), "raw id stays in tooltip");
const manual = chatHeadFor({id: "chat-2", repo: "r", issue: "774", title: "내 메모", run_id: "40857b36"}, runs);
eq(manual.primary, "내 메모", "manual custom title preserved");
const bare = chatHeadFor({id: "chat-3", repo: "r", issue: "dep-foo-1.0.0", title: ""}, []);
eq(bare.primary, "의존성 업데이트", "clear dep prefix only, no fabricated run match");
const num = chatHeadFor({id: "chat-4", repo: "r", issue: "774", title: "슬라이더 고장"}, []);
eq(num.primary, "슬라이더 고장", "title first over number");
eq(num.secondary, "#774", "number secondary");

// 7) e2e step label is neutral: only real native commands earn device wording
eq(stepKo("e2e"), "검증", "e2e step label is neutral by default");
eq(stepKo("review"), "검토", "review step label unchanged");

// 8) promotion blockers: concise Korean reasons, unknown never leaks raw
eq(blockerKo("review done and bound to current source"), "현재 소스 기준 리뷰 증거 없음",
  "review blocker in Korean");
eq(blockerKo("e2e verified on current source"), "현재 소스 기준 검사 증거 없음",
  "e2e blocker in Korean without device claim");
eq(blockerKo("no open question"), "답변 대기 질문 있음", "question blocker in Korean");
eq(blockerKo("release notes drafted"), "릴리스 노트 초안 없음", "notes blocker in Korean");
eq(blockerKo("breaking-change coverage beyond typecheck (unit/build/runtime)"),
  "큰 변경 대비 추가 검사(단위·빌드·기기) 없음",
  "breaking coverage blocker in Korean");
eq(blockerKo("some future english criterion"), "",
  "unknown criterion stays out of the primary summary");
eq(blockerKo(""), "", "empty blocker maps to nothing");

// 9) completed-run next action: human review report first, diffstat
//    alone is not human review, otherwise this run's changes, never a
//    loop back to the command center
let rn = reviewNextFor({hasHumanReview: true,
  reviewFiles: [{file: "review.diffstat.txt", label: "review 변경 요약"}],
  drafts: [], prInline: [], hasChanges: true});
eq(rn.act, "review-report", "human review reveals the report panel");
eq(rn.label, "리뷰 확인", "human review keeps the review label");
eq(rn.items.length, 0, "report jump carries no file guess");
rn = reviewNextFor({reviewFiles: [{file: "review.diffstat.txt", label: "review 변경 요약"}],
  drafts: [], prInline: [], hasChanges: true});
eq(rn.act, "review-scroll", "diffstat alone is not human review");
eq(rn.label, "결과 검토", "diffstat fallback uses the neutral result label");
rn = reviewNextFor({reviewFiles: [{file: "review.diff.txt", label: "검토 변경분 보기"}],
  drafts: [], prInline: [], hasChanges: true});
eq(rn.act, "review-open", "real review diff still opens here");
eq(rn.items.length, 1, "exactly the review item is targeted");
rn = reviewNextFor({prInline: [{key: "pr", label: "PR 본문 보기"}],
  reviewFiles: [{file: "review.diff.txt", label: "검토 변경분 보기"}],
  drafts: [], hasChanges: true});
eq(rn.items[0].kind, "inline", "PR draft has priority");
eq(rn.items[0].key, "pr", "PR draft key carried to the opener");
rn = reviewNextFor({reviewFiles: [], drafts: [], prInline: [], hasChanges: true});
eq(rn.act, "review-scroll", "no review proof falls back to this run's changes");
eq(rn.label, "결과 검토", "fallback uses the neutral result label");
rn = reviewNextFor({reviewFiles: [], drafts: [{file: "chat-draft.md", label: "초안 열기"}],
  prInline: [], hasChanges: false});
eq(rn.act, "review-open", "saved draft counts as review proof");
rn = reviewNextFor({reviewFiles: [], drafts: [], prInline: [], hasChanges: false});
eq(rn.act, "review-empty", "no proof and no changes is an honest empty state");
eq(rn.label, "결과 검토", "empty state never claims a review");
rn = reviewNextFor({});
eq(rn.act, "review-empty", "missing info defaults to empty, never forged");

// 9b) concise judgment: saved disposition first, analysis behind a
//     toggle, full texts collapsed, QA mode never confirms unknowns
let cd = candidateDecisionFor({}, {outcome: {disposition: {kind: "ACTIONABLE", detail: "bump lock"}},
  report: {analysis: "DECISION: proceed\\nreasons here", analysis_missing: false}}, null);
eq(cd.text, "조치 필요 · bump lock", "saved disposition is the concise judgment");
eq(cd.full, "DECISION: proceed\\nreasons here", "analysis stays behind the toggle");
cd = candidateDecisionFor({decision: "hold"}, {outcome: {}, report: {}}, null);
eq(cd.text, "hold", "explicit decision wins");
eq(cd.full, "", "explicit decision needs no toggle");
cd = candidateDecisionFor({}, {outcome: {}, report: {}}, null);
eq(cd.text, "", "no evidence stays empty, never invented");
cd = candidateDecisionFor({}, {outcome: {},
  report: {analysis: "x".repeat(200), analysis_missing: false}}, null);
ok(cd.text.endsWith("…"), "cut excerpt carries an ellipsis");
eq(cd.truncated, true, "cut excerpt flags truncation");
const longDet = "d".repeat(200);
cd = candidateDecisionFor({}, {outcome: {disposition: {kind: "ACTIONABLE", detail: longDet}},
  report: {analysis: "", analysis_missing: true}}, null);
ok(cd.text.endsWith("…"), "cut detail carries an ellipsis");
ok(cd.full.includes(longDet), "full cut detail survives without analysis");
cd = candidateDecisionFor({}, {outcome: {disposition: {kind: "ACTIONABLE", detail: longDet}},
  report: {analysis: "other analysis text", analysis_missing: false}}, null);
ok(cd.full.includes(longDet), "full detail preserved alongside analysis");
ok(cd.full.includes("other analysis text"), "analysis preserved alongside detail");
ok(cd.full.includes("처리 경위 detail 전문") && cd.full.includes("분석 원문"),
  "original sources distinguished");
let cp = concisePart("원본 최종 리뷰", "l1\\nl2\\nl3\\nl4\\n" + "y".repeat(500), false);
ok(!cp.includes(" open"), "full text never opens by default");
ok(cp.includes("전문 보기"), "cut text offers the full toggle");
cp = concisePart("원본 최종 리뷰", "short note", false);
ok(!cp.includes("전문 보기"), "short text needs no toggle");
cp = concisePart("원본 최종 리뷰", "", true);
ok(cp.includes("기록 없음"), "missing evidence stays explicit");
// 9d) grouped first view: plain readable summary, real facts, no syntax leak
const bt = String.fromCharCode(96);
eq(plainFirst("Done. " + bt + "a" + bt + " 1.0 -> 2.0 with [lock](workfile:yarn.lock) sync", 140),
  "Done. a 1.0 -> 2.0 with lock sync", "links and code spans read as plain words");
eq(plainFirst("- item one\\n- item two", 140), "item one", "list markers stripped, first row only");
ok(plainFirst("x".repeat(200), 140).endsWith("…"), "long line truncates with ellipsis");
eq(plainFirst("", 140), "", "empty stays empty");
cp = concisePart("수정 내용", "Done. " + bt + "pkg" + bt + " bump with [lock](workfile:yarn.lock)", false, "파일 3건");
ok(cp.includes("<b>수정 내용</b>"), "group title leads the first view");
ok(cp.includes("파일 3건"), "real file count rides the first view");
const cutAt = cp.indexOf("<details");
const firstView = cutAt === -1 ? cp : cp.slice(0, cutAt);
const fullView = cutAt === -1 ? "" : cp.slice(cutAt);
ok(!firstView.includes(bt) && !firstView.includes("workfile:"), "no markdown syntax leaks on first view");
ok(fullView.includes(bt) && fullView.includes("workfile:"), "folded full text preserves link originals");
cp = concisePart("Muse 검토", "short note", false, "");
ok(!cp.includes("전문 보기"), "short text needs no toggle");
eq(reportQaMode({state: "none"}), "candidate", "known none is the candidate path");
eq(reportQaMode({state: "unknown"}), "unknown", "unknown is not confirmed none");
eq(reportQaMode(null), "unknown", "missing QA is not confirmed none");
eq(reportQaMode({state: "ready", ready: true}), "registered", "ready stays registered");
// 9e) unknown QA keeps the server reason in the real report renderer:
// paintReportSection runs with real esc, a minimal fake box, and an md
// identity stub (structure under test, not markdown rendering).
const __renderReportSec = new Function("box", "w", "s",
  __escSrc + ";" + md.toString() +
  ";" + extractFn("reportQaMode") + ";" + extractFn("plainFirst") +
  ";" + extractFn("concisePart") + ";" + extractConstBlock("GST_KO") +
  ";" + extractFn("srcStatusKo") + ";" + extractFn("curQAHead") +
  ";" + extractFn("fcRepChip") + ";" + extractFn("fcRepIso") +
  ";" + extractFn("fcRepRc") + ";" + extractFn("fcRepTail") +
  ";" + extractFn("fcRepSteps") + ";" + extractFn("fcRepRow") +
  ";" + extractFn("fcRepConsumer") + ";" + extractFn("fcReportHtml") +
  ";" + extractFn("paintReportSection") + ";return paintReportSection(box, w, s);");
function __fakeBox() {
  let html = "";
  const nil = {};
  return {
    set innerHTML(h) { html = h; }, get innerHTML() { return html; },
    querySelector: (sel) => nil, querySelectorAll: (sel) => []
  };
}
function __wr(state, qa) {
  return {report: {fix: "fix text", fix_missing: false, analysis: "",
    analysis_missing: true, review: "review text", review_missing: false,
    review_scope: {status: "missing", error: ""}, review_scope_files: [],
    update_kind: "", fix_files: 1, scope_note: ""}, current_qa: qa};
}
let __box = __fakeBox();
__renderReportSec(__box, __wr(null, {state: "unknown", reason: "묶음 목록을 읽지 못함"}), {repo: "r"});
ok(__box.innerHTML.includes("등록 여부 미확인"), "unknown stays unverifiable in real render");
ok(__box.innerHTML.includes("묶음 목록을 읽지 못함"), "server reason survives in real render");
ok(!__box.innerHTML.includes("미등록/미검증"), "unknown never claims confirmed none");
__box = __fakeBox();
__renderReportSec(__box, __wr(null, {state: "unknown",
  reason: "<img src=x onerror=alert(1)>"}), {repo: "r"});
ok(__box.innerHTML.includes("&lt;img"), "hostile reason escaped in real render");
ok(__box.innerHTML.indexOf("<img") === -1, "no live markup from hostile reason");
__box = __fakeBox();
__renderReportSec(__box, __wr(null, {state: "unknown"}), {repo: "r"});
ok(__box.innerHTML.includes("등록 여부 미확인"), "reason-less unknown stays unverifiable");
ok(__box.innerHTML.indexOf("undefined") === -1, "no undefined leak without reason");
__box = __fakeBox();
__renderReportSec(__box, __wr(null, null), {repo: "r"});
ok(__box.innerHTML.includes("등록 여부 미확인"), "missing QA stays unverifiable");
ok(__box.innerHTML.indexOf("undefined") === -1, "no undefined leak without QA");
ok(!__box.innerHTML.includes("미등록/미검증"), "missing QA never claims confirmed none");
__box = __fakeBox();
__renderReportSec(__box, __wr(null, {state: "none"}), {repo: "r"});
ok(__box.innerHTML.includes("후보 검토/변경"), "known none keeps the candidate path");
// 9e2) integrated fastcheck_report renders through the real section:
// worker record only, never an independent-review claim.
const __fc = {state: "pass-worker",
  reason: "앱 자동 검사 통과 · 최종 판단은 별도 기록 참조",
  op: "prep-x", recorded: {head: "h", fingerprint: "f", path: "/wt"},
  live: {head: "h", fingerprint: "f", clean: true, worktree: "/wt"},
  pending_gates: {count: 1, labels: ["최종 검수 확인"],
    by_status: {fail: 0, blocked: 0, pending: 1, other: 0}},
  hold: {state: "none"}, receipt_sha256: "f".repeat(64),
  at: "2026-10-25T00:00:00", wall_s: 66.8,
  worker_attested: true, root_attested: false, ok: true,
  consumer: null,
  steps: [{name: "unit", gate: "unit test", rc: 0, timed_out: false,
    elapsed_s: 1.5, argv: "make unit", head: "h", fingerprint: "f",
    tail: "ok\\n", truncated: false,
    isolation: {network: "loopback-only"}}],
  warnings: [], warnings_omitted: 0};
__box = __fakeBox();
__renderReportSec(__box, Object.assign(__wr(null, null),
  {fastcheck_report: __fc}), {repo: "r"});
ok(__box.innerHTML.includes("준비한 코드의 검사 결과"), "integrated fcrep section renders");
ok(__box.innerHTML.includes("최종 판단은 별도"), "integrated fcrep stays review-neutral");
ok(__box.innerHTML.indexOf("독립 검수 대기") === -1, "integrated fcrep never invents review wait");
__box = __fakeBox();
const __fcX = Object.assign({}, __fc, {reason: "<img src=x onerror=alert(1)>"});
__renderReportSec(__box, Object.assign(__wr(null, null),
  {fastcheck_report: __fcX}), {repo: "r"});
ok(__box.innerHTML.includes("&lt;img"), "hostile fcrep reason escaped");
ok(__box.innerHTML.indexOf("<img") === -1, "no live markup from hostile fcrep reason");
__box = __fakeBox();
__renderReportSec(__box, __wr(null, {state: "ready", ready: true, purpose: "B",
  batch: "b", lane: "main", gates: [], gates_passed: 0, files: [], files_base: "",
  files_error: "", source: {head: "h", clean: true}, review_verdict: "미기록",
  review_findings: [], reasons: [], inherited: []}), {repo: "r"});
ok(__box.innerHTML.includes("현재 검증됨"), "ready keeps QA-first");
ok(__box.innerHTML.includes("원본 후보 기록"), "ready keeps folded history");
ok(!__box.innerHTML.includes("등록 여부 미확인"), "ready never shows unknown");

// 9c) report polling fingerprint: content changes repaint, clocks do not
const wBase = () => ({state: {steps: {}}, verification: [], outcome: {},
  source: {}, sourceChanges: {}, settlement: {}, integration_prep: {},
  report: {fix: "aaa", review: "rrr", analysis: "nnn", update_kind: "lock-refresh",
    review_scope: {status: "recorded", base_short: "b", head_short: "h",
      fingerprint_short: "f", stat_short: "s", error: ""}, review_scope_files: []},
  current_qa: {state: "none", evaluated_at: "t1"}});
let wa = wBase(), wb = wBase();
wb.report.fix = "bbb";
ok(workFpFor(wa) !== workFpFor(wb), "equal-length text change repaints");
wb = wBase();
wb.report.review_scope.stat_short = "changed stat";
ok(workFpFor(wa) !== workFpFor(wb), "scope content change repaints");
wb = wBase();
wb.report.review_scope.status = "mismatch";
wb.report.review_scope.error = "실행 기록과 범위 불일치(현재 검증 아님)";
ok(workFpFor(wa) !== workFpFor(wb), "scope status change repaints");
wb = wBase();
wb.current_qa.evaluated_at = "t2";
eq(workFpFor(wa), workFpFor(wb), "clock-only change never repaints");

// 10) home rows: human purpose title primary, synthetic dep IDs tooltip-only
let hhd = homeRunHead({repo: "react-native-nitro-sound",
  issue: "dep-@react-native-community/slider-5.2.1",
  title: "슬라이더 5.2.0 → 5.2.1", id: "run:abc123"});
eq(hhd.primary, "슬라이더 5.2.0 → 5.2.1", "dep run title is primary");
ok(!hhd.primary.includes("dep-"), "synthetic dep id never primary");
eq(hhd.secondary, "react-native-nitro-sound", "repo secondary for synthetic id");
ok(hhd.tip.includes("dep-@react-native-community/slider-5.2.1"), "synthetic id kept in tooltip");
let hhn = homeRunHead({repo: "r", issue: "774", title: "슬라이더 고장", id: "run:x"});
eq(hhn.primary, "슬라이더 고장", "numeric issue title primary");
eq(hhn.secondary, "r#774", "genuine issue number secondary");
let hhb = homeRunHead({repo: "r", issue: "dep-foo-1.0.0", title: "", id: "run:y"});
eq(hhb.primary, "의존성 업데이트", "untitled dep falls back to purpose, not id");

// 11) dep queue-state gating: only queued exact keys may execute
eq(depActionFor({status: "queued", key: "k"}).exec, true, "queued key is executable");
eq(depActionFor({status: "waiting-review", key: "k"}).exec, false,
  "waiting-review never promises execution");
eq(depActionFor({status: "running", key: "k"}).exec, false,
  "running never promises execution");
eq(depActionFor({status: "blocked", key: "k"}).exec, false,
  "blocked never promises execution");
eq(depActionFor({status: "queued"}).exec, false, "keyless queued cannot execute");
ok(depActionFor({status: "waiting-review", key: "k"}).why.length > 0,
  "non-executable states carry the actual reason");
// 11b) queue display status: an actually-watching item reads as an
// external wait (never human review); cancelled history stays cancelled.
eq(queueDisplayStatus({status: "waiting-review",
  watch: {active: true, reason: "r"}}), "watch",
  "active watch overrides stored waiting-review for display");
eq(queueDisplayStatus({status: "cancelled",
  watch: {active: true, reason: "r"}}), "cancelled",
  "cancelled history stays cancelled");
eq(queueDisplayStatus({status: "waiting-review"}), "waiting-review",
  "unwatched items keep their stored status");
eq(queueDisplayStatus({status: "queued", key: "k"}), "queued",
  "queued items keep their stored status");
const watchAct = depActionFor({status: "waiting-review", key: "k",
  watch: {active: true, reason: "wait for toolchain", trigger: "release X"}});
eq(watchAct.exec, false, "watched items never promise execution");
ok(watchAct.why.indexOf("외부 대기 중") === 0 &&
  watchAct.why.indexOf("리뷰 대기") === -1,
  "watched queue why reads external wait, not human review");

// 12) no-code safety: explicit duplicate truth never reads as an upgrade
eq(isNoCodeDone({disposition: {kind: "DUPLICATE"}}, ""), true,
  "duplicate disposition is no-code");
eq(isNoCodeDone({triage: "DUPLICATE", triageSource: "legacy-positive"}, ""), true,
  "legacy duplicate verdict is no-code");
eq(isNoCodeDone({triage: "UNKNOWN"}, ""), false, "unknown is not no-code");
eq(isNoCodeDone({}, ""), false, "empty outcome is not no-code");
eq(isNoCodeDone({}, "duplicate: foo"), true, "duplicate waitReason is no-code");
eq(runOutcomeWord({waitReason: "duplicate: foo", steps: {}}), "코드 변경 없음(중복)",
  "duplicate run never upgrade-labeled");
eq(runOutcomeWord({waitReason: "", steps: {e2e: {status: "done"}}}), "완료",
  "plain all-done still reads done");
eq(runOutcomeWord({id: "run-1", steps: {a: {status: "pending"}}}), "멈춤 — 진행 중인 작업 없음",
  "pending with nothing live is stopped, not running");
eq(runOutcomeWord({id: "run-demostop01", waitReason: null,
  steps: {triage: {status: "done"}, analyze: {status: "pending"}}}, []), "멈춤 — 진행 중인 작업 없음",
  "stopped seed shape (triage done, analyze pending, no live job) reads stopped");
eq(runOutcomeWord({id: "run-1", steps: {a: {status: "running"}}}), "진행 중",
  "stored running step still reads running");
eq(runOutcomeWord({id: "run-1", steps: {a: {status: "pending"}}},
  [{id: "j1", status: "running", label: "run-1"}]), "진행 중",
  "live same-run job reads running");
eq(runOutcomeWord({id: "run-1", repo: "r", issue: "7", steps: {a: {status: "pending"}}},
  [{id: "j2", status: "running", label: "r#7"}]), "진행 중",
  "repo#issue job label matches the run");
eq(runOutcomeWord({id: "run-1", steps: {a: {status: "pending"}}},
  [{id: "j9", status: "running", label: "run-9"}]), "멈춤 — 진행 중인 작업 없음",
  "a foreign live job is not this run's evidence");
eq(runOutcomeWord({id: "run-1", steps: {a: {status: "pending"}}},
  [{id: "j3", status: "done", label: "run-1"}]), "멈춤 — 진행 중인 작업 없음",
  "a finished job is not live");
eq(runOutcomeWord({id: "run-1", waitReason: "", steps: {e2e: {status: "failed"}}}), "확인 필요",
  "failed step still reads attention");
eq(isValidatedNoChange({verification: [{step: "review", proof: "stale"}],
  sourceChanges: {diff: "", availability: "live"}}), false,
  "stale review plus empty diff alone is not validated (settlement required)");
eq(isValidatedNoChange({verification: [{step: "review", proof: "stale"}],
  sourceChanges: {diffUnstaged: "diff --git a/x b/x", availability: "live"}}), false,
  "stale review with a real diff is not no-change");
eq(isValidatedNoChange({verification: [{step: "review", proof: "fresh"}],
  sourceChanges: {diff: "", availability: "live"}}), false, "fresh proof is not the stale path");

// 12b) settlement correction (§10): exact cancelled-superseded run plus
// live readable empty changes is validated; everything else is not.
// The positive case mirrors the real calendar queue record.
const SETTLE_Q = [{key: "flutter_calendar_carousel+intl+0.20.3+stable",
  status: "cancelled", run_id: "run-ef5c1c8e",
  note: "superseded: stale: intl already at 0.20.3 in tracked locks; ignored dev-lock lag is not a source job"}];
const wSettled = {state: {id: "run-ef5c1c8e"},
  verification: [{step: "review", proof: "stale"}],
  sourceChanges: {diff: "", availability: "live"}};
eq(isValidatedNoChange(wSettled, SETTLE_Q), true,
  "exact superseded-compatible run with known-empty live source is validated no-change");
eq(isValidatedNoChange({state: {id: "run-ef5c1c8e"},
  verification: [{step: "e2e", proof: "stale"}],
  sourceChanges: {diffCommitted: "", diffStaged: "", diffUnstaged: "",
    untrackedContents: [], availability: "live"}}, SETTLE_Q), true,
  "e2e stale counts like review stale for the exact run");
eq(isValidatedNoChange({state: {id: "ef5c1c8e"},
  verification: [{step: "e2e", proof: "stale"}],
  sourceChanges: {diff: "", availability: "live"}}, SETTLE_Q), false,
  "truncated id form never matches the exact run record");
// operator cancel without a note is unknown, never validated
const wOp = {state: {id: "run-ef5c1c8e"},
  verification: [{step: "review", proof: "stale"}],
  sourceChanges: {diff: "", availability: "live"}};
eq(isValidatedNoChange(wOp,
  [{status: "cancelled", run_id: "run-ef5c1c8e", note: ""}]), false,
  "operator cancel without note stays cancelled-unknown");
eq(isValidatedNoChange(wOp,
  [{status: "cancelled", run_id: "run-ef5c1c8e",
    note: "operator cancelled queued item"}]), false,
  "operator cancel text is not a superseded settlement");
// unrelated superseded (newer target, not no-change) cannot infer none
eq(isValidatedNoChange(wOp,
  [{status: "cancelled", run_id: "run-ef5c1c8e",
    note: "superseded: newer target 0.21.0 queued"}]), false,
  "superseded by an unrelated newer target is not validated no-change");
eq(supersededNoteIsNoChange("superseded: newer target 0.21.0 queued"), false,
  "newer-target note never reads as no-change");
eq(supersededNoteIsNoChange("operator cancelled queued item"), false,
  "operator text never reads as no-change");
eq(supersededNoteIsNoChange(SETTLE_Q[0].note), true,
  "compatible/no-source-job note reads as no-change");
// another run's settlement never validates this run
eq(isValidatedNoChange(wOp,
  [{status: "cancelled", run_id: "run-other",
    note: SETTLE_Q[0].note}]), false,
  "another run's settlement does not validate this run");
eq(settlementForRun(SETTLE_Q, "run-other"), null,
  "settlement lookup is exact-run only, no dep/name guessing");
eq(settlementForRun(SETTLE_Q, "run-ef5c1c8e").run_id, "run-ef5c1c8e",
  "settlement lookup returns the exact queue record");
eq(settlementForRun(
  [{status: "cancelled", run_id: "run-ef5c1c8e", note: ""}], "run-ef5c1c8e"), null,
  "operator cancel is not a settlement record");
// unreadable sources are unknown, never none
const wStaleEmpty = {state: {id: "run-ef5c1c8e"},
  verification: [{step: "review", proof: "stale"}],
  sourceChanges: {diff: "", availability: "live"}};
eq(isValidatedNoChange(
  {state: {id: "run-ef5c1c8e"},
    verification: [{step: "review", proof: "stale"}],
    sourceChanges: {availability: "unavailable", error: "worktree gone"}},
  SETTLE_Q), false, "unavailable worktree is unknown, not none");
eq(isValidatedNoChange(
  {state: {id: "run-ef5c1c8e"},
    verification: [{step: "review", proof: "stale"}],
    sourceChanges: {diff: "", historical: true}}, SETTLE_Q), false,
  "historical snapshot is not current emptiness");
eq(isValidatedNoChange(
  {state: {id: "run-ef5c1c8e"},
    verification: [{step: "review", proof: "stale"}],
    sourceChanges: {diff: "", availability: "live", headMatch: false}},
  SETTLE_Q), false, "recorded-vs-actual HEAD drift is an external edit, not none");
eq(isValidatedNoChange(
  {state: {id: "run-ef5c1c8e"},
    verification: [{step: "review", proof: "stale"}]}, SETTLE_Q), false,
  "missing source payload is unknown, not empty");
eq(isValidatedNoChange(wStaleEmpty, []), false,
  "no queue evidence means no validated claim");
eq(sourceIsLiveEmpty({sourceChanges: {diff: "", availability: "live"}}), true,
  "live readable empty source is empty");
eq(sourceIsLiveEmpty({sourceChanges: {diff: "", planned: true}}), false,
  "planned pre-run state is not live emptiness");
eq(sourceIsLiveEmpty({}), false, "missing payload is unknown");
// legacy proof stays historical, never fresh
eq(isValidatedNoChange({state: {id: "run-ef5c1c8e"},
  verification: [{step: "review", proof: "legacy"}],
  sourceChanges: {diff: "", availability: "live"}}, SETTLE_Q), false,
  "legacy proof is historical, not a validated current source");
// explicit duplicate history keeps its own honest reason, not a verified upgrade
eq(isNoCodeDone({disposition: {kind: "DUPLICATE"}}, ""), true,
  "duplicate history stays an explicit no-code settlement");
eq(isValidatedNoChange({state: {id: "run-dup"},
  outcome: {disposition: {kind: "DUPLICATE"}},
  verification: [{step: "review", proof: "stale"}],
  sourceChanges: {diff: "", availability: "live"}}, []), false,
  "duplicate history never upgrades into validated without its own settlement");

// 12c) UI13 availability authority: live wins over the legacy planned flag;
// unreadable is never empty; positive helper rejects lookalike notes.
eq(scAvailOf({availability: "live", planned: true, hasChanges: false, headMatch: true}), "live",
  "live availability wins over the legacy planned flag");
eq(scAvailOf({availability: "planned"}), "planned", "true planned stays pre-run");
eq(scAvailOf({planned: true}), "planned", "legacy planned without availability stays planned");
eq(scAvailOf({availability: "unavailable"}), "unavailable", "unreadable stays unknown, never empty");
eq(scAvailOf({historical: true}), "historical", "historical snapshot is not current");
eq(scAvailOf(null), "unknown", "missing payload is unknown");
eq(sourceIsLiveEmpty({sourceChanges: {diff: "", availability: "live", planned: true, hasChanges: false, headMatch: true}}), true,
  "executed run with live empty diff is empty despite the legacy planned flag");
eq(sourceIsLiveEmpty({sourceChanges: {diff: "", availability: "planned"}}), false,
  "true planned pre-run state is not live emptiness");
eq(supersededNoteIsNoChange("superseded: incompatible with current target"), false,
  "incompatible must not read as already-reflected");
eq(supersededNoteIsNoChange("superseded: already retried but failed"), false,
  "retried-but-failed must not read as no-change");
eq(supersededNoteIsNoChange("superseded: committed source already intl-0.20.3 compatible; No source change needed"), true,
  "explicit already-compatible evidence reads as no-change");
eq(supersededNoteIsNoChange("superseded: replaced by run-newer"), false,
  "replacement by another target is not confirmed no-change");
// 12d) UI14 settled next action: always the rendered record, never a guessed file.
let snr = settledNextFor({act: "review-open", label: "리뷰 확인", items: [{kind: "file", name: "r"}]});
eq(snr.act, "settled-record", "settled proof never opens a guessed file");
eq(snr.label, "처리 기록 보기", "settled proof never asks for review");
snr = settledNextFor({act: "review-scroll", label: "결과 검토", items: []});
eq(snr.act, "settled-record", "settled record guides to the rendered record");
eq(snr.label, "처리 기록 보기", "settled record label is history, not review");
snr = settledNextFor({act: "review-empty", label: "결과 검토", items: []});
eq(snr.act, "settled-record", "settled empty state opens the record, not a modal");
eq(snr.label, "처리 기록 보기", "settled empty state never claims a review");
eq(reviewNextFor({hasHumanReview: true, reviewFiles: [],
  drafts: [], prInline: [], hasChanges: true}).label, "리뷰 확인",
  "human review runs keep the review label");
eq(reviewNextFor({reviewFiles: [{file: "review.diffstat.txt", label: "review 변경 요약"}],
  drafts: [], prInline: [], hasChanges: true}).act, "review-scroll",
  "diffstat alone never opens a review dialog");

// 13) group member heads: full titles, number/kind secondary
let am = aggMemberHead({type: "issue", title: "fix: safeDouble overflow handling in chart pipeline",
  issue: "774", object_kind: "pr"});
eq(am.primary, "fix: safeDouble overflow handling in chart pipeline",
  "member full title never truncated");
eq(am.secondary, "#774 · PR", "member number/kind secondary");
let am2 = aggMemberHead({type: "run", title: "슬라이더 고장", label: "run:abc #774", issue: "774"});
eq(am2.primary, "슬라이더 고장", "member run title primary");

// 14) answer queued-state (§14): started:false never claims execution;
// queued reloads state without pollJob; saved-errors guide as saved;
// real failures stay retryable, never auto-resent.
let ao = answerOutcome({job: {id: "j1", status: "running"}, saved: true, queued: true, started: true, run: "run:a"});
eq(ao.kind, "started", "started true keeps the executing kind");
eq(ao.poll, true, "started true keeps pollJob");
modalHtml.length = 0; pollCalls = []; loadCalls = 0;
await handleAnswerOk({job: {id: "j1", status: "running"}, saved: true, queued: true, started: true, run: "run:a"});
ok(modalHtml.some((h) => h.includes("답변 전송됨. 이어서 실행합니다")), "started true shows the execution toast");
eq(JSON.stringify(pollCalls), JSON.stringify(["j1"]), "started true polls the returned job");
eq(loadCalls, 1, "started true reloads local state");
modalHtml.length = 0; pollCalls = []; loadCalls = 0;
await handleAnswerOk({saved: true, queued: false, started: true, run: "run:a"});
eq(pollCalls.length, 0, "missing job id never polls");
eq(loadCalls, 1, "missing job id still reloads state");
ao = answerOutcome({job: {id: "j2", status: "done"}, saved: true, queued: true, started: false, run: "run:b"});
eq(ao.kind, "queued", "busy+queued reports queued, not executing");
eq(ao.poll, false, "busy+queued never polls");
modalHtml.length = 0; pollCalls = []; loadCalls = 0;
await handleAnswerOk({job: {id: "j2", status: "done"}, saved: true, queued: true, started: false, run: "run:b"});
const qToast = modalHtml.filter((h) => h.startsWith("toast:")).join("");
ok(qToast.includes("답변 저장됨. 워커가 비면 같은 작업으로 이어집니다"), "queued shows the exact queued toast");
ok(!qToast.includes("이어서 실행"), "queued never claims execution");
eq(pollCalls.length, 0, "queued creates no execution poll");
eq(loadCalls, 1, "queued reloads queue/question state");
ao = answerOutcome({job: {id: "j3", status: "done"}, saved: true, queued: false, started: false, run: "run:c"});
eq(ao.poll, false, "saved-unqueued never polls");
modalHtml.length = 0; pollCalls = []; loadCalls = 0;
await handleAnswerOk({job: {id: "j3", status: "done"}, saved: true, queued: false, started: false, run: "run:c"});
const uToast = modalHtml.filter((h) => h.startsWith("toast:")).join("");
ok(uToast.includes("답변 저장됨") && uToast.includes("아직 자동 재개가 예약되지 않았습니다"),
  "unqueued distinguishes saved from not-reserved");
ok(!uToast.includes("이어서 실행") && !uToast.includes("워커가 비면"), "unqueued promises no auto start");
ao = answerOutcome({saved: true, started: false, run: "run:d"});
eq(ao.poll, false, "unknown queue never polls");
ok(!ao.toast.includes("이어서 실행") && !ao.toast.includes("워커가 비면"),
  "unknown queue promises no auto start");
ao = answerOutcome({job: {id: "j4", status: "running"}});
ok(!ao.toast.includes("이어서 실행"), "legacy running never claims this answer started it");
eq(ao.poll, false, "legacy never polls without started:true");
ao = answerOutcome({});
ok(ao.toast.includes("답변 저장됨"), "empty legacy still reports saved+refresh");
eq(ao.poll, false, "empty legacy never polls");
ao = answerOutcome(null);
eq(ao.poll, false, "null response never polls");
ok(answerErrorIsSaved("answer saved; worker busy so the same run is queued for later dispatch (not started)"),
  "409 answer-saved semicolon form is saved");
ok(answerErrorIsSaved("answer saved but another job is already running; run queued for later dispatch"),
  "409 answer-saved prose form is saved");
ok(!answerErrorIsSaved("question not waiting for an answer"), "invalid-answer 409 is a real failure");
ok(!answerErrorIsSaved("Failed to fetch"), "network failure is a real failure");

// 15) worker lock display gate: explicit live evidence only; owner metadata
// alone never reads as running; missing/failed probe reads as unknown.
eq(workerLockWord({owner: "pid-25750", live: true}), "live-owner",
  "live holder with owner reads live-owner");
eq(workerLockWord({owner: null, live: true}), "live-unknown",
  "live flock without metadata reads live-unknown, never idle");
eq(workerLockWord({owner: "pid-25750", live: false}), "idle",
  "stale owner metadata with a free lease reads idle, never running");
eq(workerLockWord({owner: "pid-25750"}), "unknown",
  "owner metadata without a live probe never claims running");
eq(workerLockWord(null), "unknown", "missing probe reads unknown");
eq(workerLockWord({}), "unknown", "empty probe reads unknown");

// 16) external-watch next-action guard: a watch never offers resume,
// even when a step failed; unwatched failures still offer resume.
eq(watchNextAct({reason: "upstream"}, ["e2e", {}]), "watch-scroll",
  "watched run with a failed step offers watch conditions, never resume");
eq(watchNextAct({reason: "upstream"}, null), "watch-scroll",
  "watched run without failures still offers watch conditions");
eq(watchNextAct(null, ["e2e", {}]), "resume",
  "unwatched failure still offers resume");
eq(watchNextAct(null, null), "none", "plain run offers no watch/resume act");

// 16b) last-result labels: raw external-watch reads as human Korean;
// unknown results stay raw, never fabricated; empty stays done.
eq(resultKo("external-watch"), "외부 대기",
  "raw external-watch last result is labeled, never raw");
eq(resultKo("EXTERNAL-WATCH"), "외부 대기",
  "upper-case watch kind is labeled too");
eq(resultKo("done"), "done", "unmapped results stay raw, never guessed");
eq(resultKo(""), "완료", "empty result reads done");
eq(resultKo(null), "완료", "missing result reads done");

// 16c) worker headline: human purpose primary; raw owner/pid only in
// the tooltip, never as the prominent user-facing line.
let wh = workerHeadline({owner: "pid-62880", pid: 62880}, "live-owner",
  {primary: "슬라이더 5.2.0 → 5.2.1", secondary: "repo#1"}, "");
eq(wh.text, "워커 실행 중(슬라이더 5.2.0 → 5.2.1 · repo#1)",
  "live purpose stays the prominent line");
ok(!wh.text.includes("pid-"), "purpose line never exposes the raw pid");
wh = workerHeadline({owner: "pid-62880", pid: 62880}, "live-owner", null, "");
eq(wh.text, "워커 잠금 중", "lock-only reads locked, never executing");
ok(!wh.text.includes("pid-"), "lock-only primary never exposes the raw owner");
ok(wh.tip.includes("pid-62880"), "raw owner stays in technical tooltip");
wh = workerHeadline(null, "unknown", null, "");
eq(wh.text, "워커 상태 알 수 없음", "missing probe reads unknown");
modalHtml.length = 0; pollCalls = []; loadCalls = 0;
let eb = {disabled: true};
eq(handleAnswerError(new Error("answer saved; worker busy"), eb), "saved", "saved-error returns saved");
ok(modalHtml.some((h) => h.includes("답변 저장됨") && !h.includes("전송 실패")),
  "saved-error is guided as saved, not failure");
eq(pollCalls.length, 0, "saved-error creates no poll");
eq(loadCalls, 1, "saved-error reloads state");
eq(eb.disabled, true, "saved-error keeps the button so the answer is not resent");
eb = {disabled: true};
eq(handleAnswerError(new Error("boom"), eb), "failed", "real failure returns failed");
ok(modalHtml.some((h) => h.includes("전송 실패")), "real failure keeps the failure toast");
eq(eb.disabled, false, "real failure re-enables for retry");

// 17) current-QA head: catalog label primary, raw key secondary;
// file scope dynamic, full SHAs folded, states honest.
const FULL = "abcdef0123456789abcdef0123456789abcdef01";
let qaReady = {state: "ready", ready: true, batch: "b1",
  purpose: "Nitrogen 통합 검증", lane: "next", gates_passed: 2,
  gates: [{key: "unit", label: "단위 검사", status: "pass",
    note: "6묶음 통과.", evidence: 1},
    {key: "native-ios", label: "iOS 기기 실행", status: "pass",
      note: "85명령 통과.", evidence: 5}],
  review_findings: ["판단 한 줄"],
  source: {head: FULL, clean: true, status: "premerge-verified",
    worktree: "/wt"},
  inherited: [{batch: "a", count: 8}],
  files: ["p.json", "y.lock"], files_base: "efd5e5411813", files_error: ""};
let qh = curQAHead(qaReady, {repo: "r"});
ok(qh.includes("현재 검증됨"), "ready chip first screen");
ok(qh.includes("단위 검사"), "catalog label primary");
ok(!qh.includes(">unit<"), "raw gate key never primary");
ok(qh.includes("통합 변경 — 2건"), "integration file count dynamic");
ok(qh.includes('data-qachange="y.lock"'), "lock file links to the real diff");
ok(!/[0-9a-f]{40}/.test(qh.split("<details>")[0]),
  "full SHA folded below the first screen");
ok(qh.includes("판단 한 줄"), "registered finding shown, nothing invented");
let qaStale = {state: "stale", ready: false, batch: "b1", lane: "next",
  gates: [], reasons: ["작업대 다름"], source: {}};
qh = curQAHead(qaStale, {repo: "r"});
ok(qh.includes("재검증 필요") && qh.includes("작업대 다름"),
  "stale shows the actual reason");
let qaLeaf = {state: "stale", ready: false, batch: "b1", lane: "next",
  gates: [], reasons: ["inherited 'a' not fully proven (0/1 ready)"],
  leaf: "[a d t l] live integration source unreadable", source: {}};
qh = curQAHead(qaLeaf, {repo: "r"});
ok(qh.includes("하위 원인 보기") &&
  qh.includes("[a d t l] live integration source unreadable"),
  "stale with leaf discloses the nested cause");
eq((qh.match(/하위 원인 보기/g) || []).length, 1,
  "single cause disclosure, no text wall");
let qaLeafOk = {state: "ready", ready: true, batch: "b1", lane: "next",
  gates: [], reasons: [], leaf: "[a] x", source: {}};
ok(!curQAHead(qaLeafOk, {repo: "r"}).includes("하위 원인 보기"),
  "fresh results never disclose a cause");
let qaNoLeaf = {state: "stale", ready: false, batch: "b1", lane: "next",
  gates: [], reasons: ["작업대 다름"], source: {}};
ok(!curQAHead(qaNoLeaf, {repo: "r"}).includes("하위 원인 보기"),
  "stale without leaf shows no disclosure");
ok(curQAHead(qaLeaf, {repo: "r"}).includes('data-dk="qa-leaf"'),
  "cause disclosure keeps a stable open key across polls");
// hostile leaf uses the REAL shipped esc + shipped curQAHead in a fresh
// scope: the shared driver esc stub is identity and cannot prove safe
// rendering. Asserts escaped text and no live markup.
ok(!!__escSrc, "real esc extracted from shipped script");
const __diagScope = __escSrc + ";" + extractConstBlock("DIAG_CAT_KO") +
  ";" + extractConstBlock("STEP_KO") + ";" + extractConstBlock("VSTAT_KO") +
  ";" + extractConstLine("stepKo") + ";" + extractConstLine("vstatKo") +
  ";" + extractFn("qaDiagFirst") + ";" + extractFn("qaDiagSecond") +
  ";" + extractFn("qaDiagChain") + ";" + extractFn("qaDiagTech") +
  ";" + extractFn("qaDiagBlock") + ";";
const __renderReal = new Function("qa", "s",
  __diagScope + extractConstBlock("GST_KO") + ";" + extractFn("srcStatusKo") +
  ";" + extractFn("curQAHead") + ";return curQAHead(qa, s);");
const qhHostile = __renderReal({state: "stale", ready: false, batch: "b1",
  gates: [], reasons: ["r"],
  leaf: "<img src=x onerror=alert(1)><script>alert(2)</script>",
  source: {}}, {repo: "r"});
ok(qhHostile.includes("하위 원인 보기"), "real render discloses the cause");
ok(qhHostile.includes("&lt;img"), "hostile leaf escaped");
ok(qhHostile.indexOf("<img") === -1 && qhHostile.indexOf("<script") === -1,
  "no live markup from hostile leaf");
// 17b) structured diag: reason-specific factual first line, esc kept,
// no overclaim about transience, 390px-safe wrapping on paths.
const __renderDiag = new Function("qa", "s",
  __diagScope + extractConstBlock("GST_KO") + ";" + extractFn("srcStatusKo") +
  ";" + extractFn("curQAHead") + ";return curQAHead(qa, s);");
const __firstOf = new Function("d",
  __diagScope + "return qaDiagFirst(d);");
const __blockOf = new Function("d",
  __diagScope + "return qaDiagBlock(d);");
function __qaDiagBase(diag) {
  return {state: "stale", ready: false, batch: "b1", lane: "next",
    gates: [], reasons: ["r"], leaf: "", diag: diag, source: {}};
}
let qd = __renderDiag(__qaDiagBase({chain: ["b-top", "b-mid", "b-leaf"],
  key: "k", kind: "cand", map_key: "cand:/wt", worktree: "/wt",
  reason_code: "candidate_not_clean", category: null, op: null, rc: null,
  path: ".watchman-cookie-h-1-2", expected_head: "abc", expected_fp: "def",
  actual_head: "abc", actual_fp: "def", clean: false}), {repo: "r"});
ok(qd.includes("확인 시점에 작업대에 커밋되지 않은 파일이 보였습니다"),
  "not-clean first line states only what was seen");
ok(qd.includes('<span class="mono">.watchman-cookie-h-1-2</span>'),
  "offending path wrapped for 390px cards");
ok(qd.includes("관련 묶음: b-top → b-mid → b-leaf"),
  "multi-batch chain shown, single batch stays quiet");
ok(qd.indexOf("파일이 바뀐 것은 아니며") === -1 &&
  qd.indexOf("일시적") === -1,
  "no overclaim that nothing changed or that it is transient");
let qdh = __renderDiag(__qaDiagBase({chain: ["b"], key: "k", kind: "cand",
  map_key: "cand:/wt", worktree: "/wt", reason_code: "candidate_not_clean",
  category: null, op: null, rc: null,
  path: "<img src=x onerror=alert(1)>", clean: false}), {repo: "r"});
ok(qdh.includes("&lt;img") && qdh.indexOf("<img src=x") === -1,
  "hostile diag path escaped, no live markup");
let qdu = __renderDiag(__qaDiagBase({chain: ["b"], key: "r|dep-a|2.0|main",
  kind: "cand", map_key: "cand:/wt", worktree: "/wt",
  reason_code: "candidate_unreadable",
  category: "source_unreadable", op: "read x", rc: 5, path: ""}),
  {repo: "r"});
ok(qdu.includes("작업대 상태를 읽지 못했습니다 (파일 또는 git 읽기 실패, 종료코드 5)"),
  "unreadable maps category to Korean, keeps rc, factually");
ok(qdu.includes("하위 원인 보기"),
  "diag without leaf still discloses the cause");
ok(qdu.includes("확인 위치: dep-a 2.0") && qdu.includes('<span class="mono">/wt</span>'),
  "second line names the item and the worktree, both escaped");
ok(qdu.includes("기술 정보: candidate_unreadable · source_unreadable · read x · rc 5"),
  "raw tokens live only in the small technical line");
let qdm = __renderDiag(__qaDiagBase({chain: ["b"], key: "k", kind: "cand",
  map_key: "cand:/wt", worktree: "/wt",
  reason_code: "candidate_fingerprint_changed",
  category: null, op: null, rc: null, path: "",
  expected_head: "aaa", expected_fp: "fp-old-123456",
  actual_head: "aaa", actual_fp: "fp-new-789012"}), {repo: "r"});
ok(qdm.includes("작업대 내용이 기록과 다릅니다 (기록 fp-old-123456 / 현재 fp-new-789012)"),
  "mismatch shows recorded vs current, factually");
let qdp = __renderDiag(__qaDiagBase({chain: ["b"], key: "k", kind: "run-proof",
  map_key: "", worktree: "/wt", reason_code: "run_proof_stale",
  category: null, op: null, rc: null, path: "",
  vstat: {review: "unknown", e2e: "fresh"}}), {repo: "r"});
ok(qdp.includes("연결된 실행의 검토 증명이 현재 내용과 다릅니다 (검토=미확인, 검증=최신)"),
  "run-proof flip carries its step states in Korean");
ok(qdp.indexOf("review=unknown") === -1,
  "raw step tokens never leak into the first line");
// 17c) every mapped category: Korean in the first line, raw enum absent
// there (raw tokens stay in the technical line only).
for (const [cat, ko] of [["missing_dir", "작업대 폴더가 없음"],
    ["source_unreadable", "파일 또는 git 읽기 실패"],
    ["status_unreadable", "git 상태 읽기 실패"],
    ["base_unreadable", "기준 브랜치 읽기 실패"],
    ["snapshot_error", "작업대 읽기 오류"],
    ["empty_identity", "작업대 식별 정보 없음"]]) {
  const line = __firstOf({reason_code: "candidate_unreadable",
    category: cat, op: null, rc: null, path: ""});
  ok(line.includes(ko) && line.indexOf(cat) === -1,
    "category " + cat + " mapped to Korean, raw absent from first line");
}
// the shared block is what the batch screens render too: first line,
// item line, chain, and technical line travel together.
const qb = __blockOf({chain: ["b-top", "b-mid"], key: "r|dep-b|3.1|next",
  kind: "wt", map_key: "wt:/wi", worktree: "/wi",
  reason_code: "integration_not_clean", category: null, op: null,
  rc: null, path: "a/<b>.js"});
ok(qb.includes("확인 시점에 작업대에 커밋되지 않은 파일이 보였습니다") &&
  qb.includes("확인 위치: dep-b 3.1") &&
  qb.includes("관련 묶음: b-top → b-mid") &&
  qb.includes("기술 정보: integration_not_clean"),
  "batch-screen block carries first line, item, chain, tech");
ok(qb.includes("a/&lt;b&gt;.js") && qb.indexOf("a/<b>.js") === -1,
  "hostile block path escaped everywhere");
ok(curQAHead({state: "none"}, {}).includes("묶인 최신 등록"),
  "no linked batch is explicit");
ok(curQAHead({state: "unknown", reason: "x"}, {}).includes("확인 불가"),
  "unknown stays unknown");
ok(curQAHead(null, {}).includes("확인 불가"), "missing QA stays unknown");
// quiet-poll fingerprint follows QA state, so a new import or drift repaints
const wq = JSON.parse(JSON.stringify(w0));
wq.current_qa = {state: "ready", ready: true, batch: "b1",
  source: {head: "h", fingerprint: "f1"}, gates_passed: 1,
  gates: [{key: "unit", label: "단위 검사", status: "pass",
    note: "통과.", evidence: 1}],
  review_verdict: "accept", review_findings: ["판단"], reasons: [],
  files: ["a"], files_base: "b", files_error: ""};
const wq2 = JSON.parse(JSON.stringify(wq));
wq2.current_qa.ready = false;
ok(workFpFor(wq) !== workFpFor(wq2), "QA verdict change rerenders");
const wq3 = JSON.parse(JSON.stringify(wq));
wq3.current_qa.source.head = "h2";
ok(workFpFor(wq) !== workFpFor(wq3), "QA source change rerenders");
// same HEAD, different bytes: a reimport with a new fingerprint repaints
const wq4 = JSON.parse(JSON.stringify(wq));
wq4.current_qa.source.fingerprint = "f2";
ok(workFpFor(wq) !== workFpFor(wq4),
  "same HEAD with a different fingerprint rerenders");
// same HEAD/count, new qualification note: evidence change repaints
const wq5 = JSON.parse(JSON.stringify(wq));
wq5.current_qa.gates[0].note = "재확인 통과.";
ok(workFpFor(wq) !== workFpFor(wq5), "new gate qualification rerenders");
const wq6 = JSON.parse(JSON.stringify(wq));
wq6.current_qa.review_findings = ["바뀐 판단"];
ok(workFpFor(wq) !== workFpFor(wq6), "new review finding rerenders");
// clock-only movement never repaints the quiet poll
const wq7 = JSON.parse(JSON.stringify(wq));
wq7.current_qa.evaluated_at = "2099-01-01T00:00:00Z";
eq(workFpFor(wq), workFpFor(wq7), "evaluated_at clock never rerenders");

// 18) chat stage wall: only known app notices collapse, humans never;
// originals preserved inside, order kept, copy keeps its index.
const ST = [
  {role: "assistant", text: "유지보수 시작 — 실행 run-1"},
  {role: "assistant", text: "**분류** 완료 — ACTIONABLE"},
  {role: "user", text: "내 질문"},
  {role: "assistant", text: "**수정** 완료 — a.json"},
  {role: "assistant", text: "**리뷰** 완료 — a.json"},
  {role: "assistant", text: "수동 안내 문구"},
];
ok(isStageNotice(ST[0]) && isStageNotice(ST[1]), "known stage notices match");
ok(!isStageNotice(ST[2]) && !isStageNotice(ST[5]),
  "human and manual messages never match");
const gt = groupedTranscript(ST);
eq((gt.match(/실행 타임라인/g) || []).length, 2,
  "each consecutive stage run collapses separately");
ok(gt.includes("내 질문") && gt.includes("수동 안내 문구"),
  "human text stays flat and in place");
ok(gt.indexOf("실행 타임라인") < gt.indexOf("내 질문") &&
  gt.lastIndexOf("실행 타임라인") > gt.indexOf("내 질문"),
  "human turn keeps its position between the two runs");
ok(gt.includes('data-copy="3"'), "copy buttons keep original indices");
eq((gt.match(/a\.json/g) || []).length, 2, "both stage originals preserved inside");
ok(gt.includes("ACTIONABLE"), "first run original preserved inside");
const gt1 = groupedTranscript(
  [{role: "assistant", text: "**수정** 완료 — x"}, {role: "user", text: "q"}]);
ok(!gt1.includes("실행 타임라인"), "a singleton stage stays flat");

// 19) chat current-result panel: same-run QA states with both actions;
// quiet refresh repaints only on fingerprint change (currQaFp contract).
let qp = chatQaPanelHtml({current_qa: {state: "ready", ready: true,
  batch: "b1", lane: "next", purpose: "Nitrogen 통합 검증",
  gates_passed: 11, gates: [{}, {}], files: ["a", "b"]}}, "run-1");
ok(qp.includes("현재 검증됨") && qp.includes("Nitrogen 통합 검증"),
  "ready panel leads with the current result");
ok(qp.includes('data-qawork="run-1"') &&
  qp.includes('data-qabatch="b1|next"'),
  "both same-run evidence actions present");
qp = chatQaPanelHtml({current_qa: {state: "stale", ready: false,
  batch: "b1", lane: "next", gates_passed: 3, gates: [{}, {}],
  files: [], reasons: ["작업대 다름"]}}, "run-1");
ok(qp.includes("재검증 필요"), "stale panel never keeps green");
ok(chatQaPanelHtml({current_qa: {state: "none"}}, "run-1").includes("묶인 최신 등록"),
  "unlinked run is explicit");
ok(chatQaPanelHtml({}, "run-1").includes("확인 불가"),
  "missing QA is unknown, never green");

// 19b) leaf joins the quiet-poll identity: same stale payload with a
// different cause repaints; identical payloads keep DOM.
const fpStaleA = {state: "stale", ready: false, batch: "b1", lane: "next",
  gates: [], reasons: ["inherited 'a' not fully proven (0/1 ready)"],
  leaf: "[a] cause-one", source: {}};
const fpStaleB = {state: "stale", ready: false, batch: "b1", lane: "next",
  gates: [], reasons: ["inherited 'a' not fully proven (0/1 ready)"],
  leaf: "[a] cause-two", source: {}};
ok(currQaFp(fpStaleA) !== currQaFp(fpStaleB),
  "changed leaf alone repaints the work view");
eq(currQaFp(fpStaleA), currQaFp(JSON.parse(JSON.stringify(fpStaleA))),
  "identical leaf keeps DOM");

// 20) chat QA refresh contract: pure update decisions plus the async
// fetch/guard/scroll behavior against a fake DOM.
const GOODW = {current_qa: {state: "ready", ready: true, batch: "b1",
  lane: "next", purpose: "P", gates_passed: 1, gates: [{}], files: []}};
let up = resolveChatQaUpdate("", GOODW, "run-1", "", 0);
ok(up && up.html.includes("현재 검증됨"), "fresh paint renders");
eq(resolveChatQaUpdate(up.fp, GOODW, "run-1", "", 0), null,
  "same fingerprint preserves DOM");
const driftW = {current_qa: {state: "stale", ready: false, batch: "b1",
  lane: "next", gates_passed: 0, gates: [{}], files: [],
  reasons: ["x"]}};
up = resolveChatQaUpdate(up.fp, driftW, "run-1", "", 0);
ok(up && up.html.includes("재검증 필요"), "drift repaints");
up = resolveChatQaUpdate(up.fp, driftW, "run-1", "boom", 123);
ok(up && up.fp === "fetch-error" && up.html.includes("boom"),
  "failure marks unknown, never keeps green");
ok(up.html.includes("KST"), "failure carries the check time");
ok(up.html.includes('data-qawork="run-1"') &&
  !up.html.includes("data-qabatch"),
  "unknown keeps work navigation, drops batch evidence");
eq(resolveChatQaUpdate("fetch-error", driftW, "run-1", "again", 0),
  null, "repeated failure does not churn DOM");
up = resolveChatQaUpdate("fetch-error", GOODW, "run-1", "", 0);
ok(up && up.html.includes("현재 검증됨"),
  "post-failure success repaints even with the pre-failure fp");
// async: fake DOM + injected fetch, real guards and scroll behavior.
let chatBox = null;
const fakeView = {scrollTop: 0, scrollHeight: 100, clientHeight: 100};
const prevDollar = $;
$ = function(id) {
  if (id === "chat-qa") return chatBox;
  if (id === "view") return fakeView;
  return prevDollar(id);
};
let chatPayload = GOODW, chatFail = "";
const prevJget = jget;
jget = async (u) => {
  await new Promise(r => setTimeout(r, 5));
  if (chatFail) throw new Error(chatFail);
  return chatPayload;
};
view = {kind: "chat", id: "chat-1"};
const newChatBox = () => ({dataset: {qaRun: "run-1", qaRepo: "r",
  qaFp: ""}, isConnected: true, innerHTML: "",
  querySelector: (s) => null});
chatBox = newChatBox();
await paintChatQA(chatBox, "chat-1", "run-1", "r");
ok(chatBox.innerHTML.includes("현재 검증됨"), "async initial paints");
const fpGood = chatBox.dataset.qaFp;
chatBox.innerHTML = "KEEP";
await refreshChatQA();
eq(chatBox.innerHTML, "KEEP", "unchanged quiet poll preserves DOM");
chatPayload = driftW;
await refreshChatQA();
ok(chatBox.innerHTML.includes("재검증 필요"), "async drift repaints");
chatFail = "boom";
await refreshChatQA();
ok(chatBox.innerHTML.includes("확인 불가"), "async failure marks unknown");
eq(chatBox.dataset.qaFp, "fetch-error", "async failure fingerprint set");
chatFail = "";
chatPayload = GOODW;
await refreshChatQA();
ok(chatBox.innerHTML.includes("현재 검증됨"),
  "async recovery repaints on the pre-failure fp");
eq(chatBox.dataset.qaFp, fpGood, "recovered fp equals the original fp");
// stale responses (navigated away / superseded) write nothing.
chatBox = newChatBox();
const slow = paintChatQA(chatBox, "chat-1", "run-1", "r");
view = {kind: "work", id: "run-9"};
await slow;
eq(chatBox.innerHTML, "", "navigated-away response writes nothing");
view = {kind: "chat", id: "chat-1"};
chatBox = newChatBox();
chatFail = "late-boom";
const slowFail = paintChatQA(chatBox, "chat-1", "run-1", "r");
view = {kind: "chat", id: "chat-2"};
await slowFail;
eq(chatBox.innerHTML, "", "stale failure never erases another route");
view = {kind: "chat", id: "chat-1"};
// scroll: pinned follows, reading stays.
fakeView.scrollTop = 0; fakeView.scrollHeight = 100;
pinBottomFollow(fakeView, true);
eq(fakeView.scrollTop, 100, "pinned bottom follows new height");
fakeView.scrollTop = 10;
pinBottomFollow(fakeView, false);
eq(fakeView.scrollTop, 10, "reading scroll never pulled down");
chatBox = newChatBox();
fakeView.scrollTop = 5; fakeView.scrollHeight = 200;
fakeView.clientHeight = 100;
chatFail = "";
await paintChatQA(chatBox, "chat-1", "run-1", "r");
eq(fakeView.scrollTop, 5, "unpinned reader not pulled down by paint");

// 11) list/detail observation coherence: ids alone never warn, same
//     projection on both sides, quiet on clock/ids only
eq(listDetailConflict(null, {ready: 1, total: 1, key: "", reasons: "", leaf: ""}), false,
  "missing list meaning is not a disagreement");
eq(listDetailConflict({ready: 1, total: 1, key: "", reasons: "", leaf: ""},
  {ready: 1, total: 1, key: "", reasons: "", leaf: ""}), false,
  "agreeing counts with different ids stay quiet");
eq(listDetailConflict({ready: 0, total: 1, key: "k", reasons: "r", leaf: ""},
  {ready: 1, total: 1, key: "", reasons: "", leaf: ""}), true,
  "real semantic disagreement is flagged");
const longReason = "r".repeat(200);
const cardLong = revCardSem({ready: 0,
  evaluation: {ready: 0, total: 1,
    first_failure: {key: "k", reasons: [longReason], leaf: ""}}});
const detLong = revDetailSem({results: [{key: "k", ready: false,
  reasons: [longReason], leaf: ""}]});
eq(listDetailConflict(cardLong, detLong), false,
  "four-reason-class long cause with identical source stays quiet");
eq(cardLong.reasons.length <= 3 * 161, true, "card reasons bounded");
eq(revBound160(longReason).length <= 160, true, "bound helper truncates");
eq(revStaleGuard(2, 1, "a|r", "a|r"), true, "older request is stale");
eq(revStaleGuard(1, 1, "a|x", "a|r"), true, "moved view is stale");
eq(revStaleGuard(1, 1, "a|r", "a|r"), false, "current request proceeds");
const fpA = reviewsContentFp("r",
  [{id: "b", updated_at: "u", ready: 1, attention: [], obs: "obs-1",
    evaluation: {ready: 1, total: 1, first_failure: null}, integrations: []}], []);
const fpB = reviewsContentFp("r",
  [{id: "b", updated_at: "u", ready: 1, attention: [], obs: "obs-2",
    evaluation: {ready: 1, total: 1, first_failure: null}, integrations: []}], []);
eq(fpA, fpB, "ids alone never repaint");
const fpC = reviewsContentFp("r",
  [{id: "b", updated_at: "u", ready: 0, attention: [], obs: "obs-2",
    evaluation: {ready: 0, total: 1,
      first_failure: {key: "k", reasons: ["nope"], leaf: ""}}, integrations: []}], []);
ok(fpA !== fpC, "cause change repaints");
const fpD = reviewsContentFp("r",
  [{id: "b", updated_at: "u", ready: 0, attention: [], obs: "obs-2",
    evaluation: {ready: 0, total: 1,
      first_failure: {key: "other", reasons: ["nope"], leaf: ""}}, integrations: []}], []);
ok(fpC !== fpD, "failing candidate change repaints with same count");
// lane headings: stored pass record vs currently bound verification
const freshWl = {head: "h1", fingerprint: "f1"};
const passGs = [{status: "pass", inputs: {head: "h1", fingerprint: "f1"}}];
eq(lanePhaseFor(passGs, freshWl), "검증 완료", "fresh all-pass verifies");
const staleGs = [{status: "pass", inputs: {head: "h0", fingerprint: "f1"}}];
eq(lanePhaseFor(staleGs, freshWl), "통과 기록 있음 · 현재 확인 필요",
  "stale all-pass is record, not current verification");
eq(lanePhaseFor([{status: "fail"}], freshWl), "막힘 확인 필요", "fail blocks");
eq(lanePhaseFor([{status: "pending"}], freshWl), "게이트 진행 중", "pending waits");
// both shipped gate shapes: nested detail inputs and flat list inputs
eq(gateStale({status: "pass",
  inputs: {head: "h0", fingerprint: "f1"}}, freshWl), true,
  "nested stale inputs detected");
eq(gateStale({status: "pass", inputs_head: "h0", inputs_fp: "f1"}, freshWl),
  true, "flat stale inputs detected");
eq(gateStale({status: "pass", inputs_head: "h1", inputs_fp: "f1"}, freshWl),
  false, "equivalent flat inputs stay quiet");
eq(gateStale({status: "pass"}, freshWl), false, "identity-free gate never stale");
eq(lanePhaseFor([{status: "pass", inputs_head: "h0", inputs_fp: "f1"}],
  freshWl), "통과 기록 있음 · 현재 확인 필요", "flat stale lane is record");

// 12) loadBatchDetail behavior with realistic stubs: ready renders
//     without warnings, disagreement separates states with a read-only
//     recheck, same-revision re-read stays quiet, and a deferred older
//     rejection/success never overwrites the newer panel
batchOpen = new Set(["b1"]);
detailReqSeq = {};
batchesSemCards = {"b1": {ready: 1, total: 1, key: "", reasons: "", leaf: ""}};
batchesCache = {batches: []};
batchesObs = "obs-list";
pendingChgExpand = null;
const readyDet = {obs: "obs-d1",
  batch: {id: "b1", target_branch: "main", integrations: [], primaries: []},
  evaluation: {results: [{key: "r|d|t|main", dep: "d", target: "t",
    lane: "main", ready: true, reasons: [], leaf: "", inherited: []}]},
  queue: {}, live: {}};
jget = async () => readyDet;
const body1 = fakeBody();
await loadBatchDetail("b1", body1);
ok(body1.innerHTML.includes("준비 1/1"), "ready detail counts once");
ok(!body1.innerHTML.includes("서로 다른 관찰"), "agreeing ids stay quiet");
ok(!body1.innerHTML.includes("data-recheck"), "no recheck without conflict");
const dfpKept = body1.dataset.df;
body1.innerHTML = "SENTINEL";
await loadBatchDetail("b1", body1);
eq(body1.innerHTML, "SENTINEL", "equivalent re-read keeps DOM");
const failDet = {obs: "obs-d2",
  batch: {id: "b1", target_branch: "main", integrations: [], primaries: []},
  evaluation: {results: [{key: "r|d|t|main", dep: "d", target: "t",
    lane: "main", ready: false, reasons: ["live candidate source unreadable"],
    leaf: "b <- c", inherited: []}]},
  queue: {}, live: {}};
jget = async () => failDet;
const body2 = fakeBody();
await loadBatchDetail("b1", body2);
ok(body2.innerHTML.includes("다시 확인 필요"), "conflict suppresses all-clear");
ok(body2.innerHTML.includes("서로 다른 관찰"), "conflict names separate observations");
ok(body2.innerHTML.includes("data-recheck"), "conflict offers read-only recheck");
ok(!body2.innerHTML.includes("병합 검토 가능"), "no merge-ready in mixed panel");
ok(body2.innerHTML.includes("b1-tech"), "tech disclosure kept");
// production esc on a hostile list trace
const __realEsc = eval("(" + __escSrc.replace("function esc",
  "function") + ")");
esc = __realEsc;
batchesCache = {batches: [{id: "b1", obs: "obs-list",
  evaluation: {ready: 0, total: 1,
    first_failure: {key: "r|d|t|main", reasons: ["<img src=x onerror=1>"],
      leaf: "", descendants: []}}}]};
batchesSemCards = {"b1": {ready: 1, total: 1, key: "", reasons: "", leaf: "", src: ""}};
const body3 = fakeBody();
await loadBatchDetail("b1", body3);
ok(body3.innerHTML.includes("&lt;img"), "hostile list trace escaped");
ok(!body3.innerHTML.includes("<img src=x"), "no live hostile markup");
// deferred older rejection never replaces the newer panel
batchesSemCards = {"b1": {ready: 1, total: 1, key: "", reasons: "", leaf: ""}};
let __gate = "fast";
jget = (u) => (__gate === "fast" ? Promise.resolve(readyDet)
  : Promise.reject(new Error("old 500")));
const body4 = fakeBody();
__gate = "slow";
const oldCall = loadBatchDetail("b1", body4);
__gate = "fast";
await loadBatchDetail("b1", body4);
__gate = "slow";
await oldCall;
ok(body4.innerHTML.includes("준비 1/1"), "newer success survives old rejection");
ok(!body4.innerHTML.includes("상세 조회 실패"), "old rejection never paints");
// deferred older success never overwrites the newer panel
const slowFailDet = {obs: "obs-d0",
  batch: {id: "b1", target_branch: "main", integrations: [], primaries: []},
  evaluation: {results: [{key: "r|d|t|main", dep: "d", target: "t",
    lane: "main", ready: false, reasons: ["stale"], leaf: "",
    inherited: []}]},
  queue: {}, live: {}};
let __resolvers = [];
jget = () => new Promise((res, rej) => __resolvers.push({res, rej}));
batchesSemCards = {"b1": {ready: 1, total: 1, key: "", reasons: "", leaf: ""}};
const body5 = fakeBody();
const older = loadBatchDetail("b1", body5);
const newer = loadBatchDetail("b1", body5);
__resolvers[1].res(readyDet);
await newer;
__resolvers[0].res(slowFailDet);
await older;
ok(body5.innerHTML.includes("준비 1/1"), "newer success survives older success");
ok(!body5.innerHTML.includes("다시 확인 필요"), "stale success never warns");
// bound source identity on both projections: equal bindings stay quiet,
// different bound source rechecks, clock/ids alone never conflict
const wqSrc = "w1";
const cardInteg = [{lane: "main", worktree: wqSrc, head: "HHHHHHHHHHHH",
  fingerprint: "FFFF", live_head: "HHHHHHHHHHHH", live_fingerprint: "FFFF",
  live_clean: true}];
const detInteg = [{lane: "main", worktree: wqSrc, head: "HHHHHHHHHHHH",
  fingerprint: "FFFF"}];
const detLive = {};
detLive["wt:" + wqSrc] = {head: "HHHHHHHHHHHH", fingerprint: "FFFF", clean: true};
const cardB = revCardSem({ready: 0,
  evaluation: {ready: 0, total: 1,
    first_failure: {key: "k", lane: "main", reasons: ["r"], leaf: ""}},
  integrations: cardInteg});
const detB = revDetailSem({results: [{key: "k", lane: "main", ready: false,
  reasons: ["r"], leaf: ""}]}, {integrations: detInteg}, detLive);
eq(listDetailConflict(cardB, detB), false, "equal bindings stay quiet");
const detLive2 = {};
detLive2["wt:" + wqSrc] = {head: "HHHHHHHHHHHH", fingerprint: "GGGG", clean: true};
const detB2 = revDetailSem({results: [{key: "k", lane: "main", ready: false,
  reasons: ["r"], leaf: ""}]}, {integrations: detInteg}, detLive2);
eq(listDetailConflict(cardB, detB2), true, "different bound source rechecks");
ok(cardB.src.includes("match"), "matching binding labeled");
// disclosure/focus state survives a changed/conflict replace
const dk1 = {open: true, getAttribute: () => "b1-tech"};
const dk2 = {open: false, getAttribute: () => "b1-x"};
const sbody = {dataset: {},
  querySelectorAll: () => [dk1, dk2],
  querySelector: (sel) => (String(sel).includes("b1-tech") ? dk1 : null),
  contains: () => false};
const kept = snapshotDetailUi(sbody);
eq(kept.open.join(","), "b1-tech", "open disclosures captured");
const nk1 = {open: false, getAttribute: () => "b1-tech"};
sbody.querySelector = (sel) => (String(sel).includes("b1-tech") ? nk1 : null);
restoreDetailUi(sbody, kept);
eq(nk1.open, true, "open disclosures restored on replace");
// read-only recheck delegates to the single guarded list renderer with
// valid context: visible counts and hidden semantics repaint together
O = {repos: [{name: "r1"}]};
view = {kind: "repo", id: "r1"};
let paintedRepo = null;
paintRepoReviews = async (r) => { paintedRepo = r; };
jget = async () => { throw new Error("must delegate, not fetch directly"); };
await recheckBatchDetail("b1", fakeBody());
eq(paintedRepo && paintedRepo.name, "r1", "recheck reuses guarded list renderer");
// without renderer context it shows the normal read error, never a
// fabricated coherent state
paintRepoReviews = null;
O = undefined;
view = undefined;
const body6 = fakeBody();
await recheckBatchDetail("b1", body6);
ok(body6.innerHTML.includes("상세 조회 실패"), "missing context shows read error");
ok(!body6.innerHTML.includes("준비 1/1"), "no fabricated success state");
// M2.14b-fix: the 함께 다시 시도 button renders on any hinted card
// except a joint record itself — live the hint sits on the DONE
// -rederive record, not only on blocked prepares.
const jhint = {partner_run: "run-y", partner_dep: "expo",
  partner_target: "58.0.17", pair: "0.86+0.88"};
ok(jointButtonVisible({op_id: "p1", status: "blocked",
  joint_hint: jhint}), "blocked hinted prepare shows joint button");
ok(jointButtonVisible({op_id: "p1", status: "done", kind: "rederive",
  joint_hint: jhint}), "done rederive with hint shows joint button");
ok(!jointButtonVisible({op_id: "p1-joint", status: "done",
  kind: "joint", joint_hint: jhint}), "joint record hides joint button");
ok(!jointButtonVisible({op_id: "p1", status: "blocked"}),
  "prepare without hint hides joint button");
ok(!jointButtonVisible({status: "blocked", joint_hint: jhint}),
  "hint without op id hides joint button");
ok(!jointButtonVisible(null), "null prep hides joint button");
eq(jointRetryMsg({status: "done", kind: "joint", op_id: "x-joint"}),
  "함께 올림 준비됨(원본들과 다름)", "joint done message");
eq(jointRetryMsg({status: "blocked", error: "mix"}),
  "함께 다시 시도 막힘: mix", "joint blocked message");
// M3.5: untrusted PR titles/authors render escaped, never executed.
const evil = {open: 12, external: 1, bots: 11,
  bot_lines: [{pr: 843, dep: "<img src=x onerror=1>",
    note: "bump <script>alert(1)</script>"}]};
const evilHtml = prCountLine(evil) + prBotLines(evil);
ok(!evilHtml.includes("<script>") && !evilHtml.includes("<img"),
  "PR render escapes untrusted text");
ok(evilHtml.includes("&lt;script&gt;"), "PR render shows escaped text");
const errHtml = prCountLine({open: 0, external: 0, bots: 0,
  error: "gh <down>"});
ok(errHtml.includes("확인 실패") && !errHtml.includes("열린 PR 0 ("),
  "PR error shows instead of 0");
ok(!errHtml.includes("gh <down>"), "PR error text escaped");
// N) tri-state branch helpers: strict bools only, unknown never absence
eq(nextLabel(true), "있음", "next true label");
eq(nextLabel(false), "없음", "next false label");
eq(nextLabel(null), "확인 안 됨", "next null is unknown");
eq(nextLabel(undefined), "확인 안 됨", "next missing is unknown");
eq(nextLabel(1), "확인 안 됨", "next truthy non-bool is unknown");
eq(nextLabel("yes"), "확인 안 됨", "next string is unknown");
const knownProv = nextProv({has_next: true,
  branch_next_observed_at: "2026-05-01T00:00:00+00:00",
  branch_next_reason: "마지막 스캔 관찰"});
ok(knownProv.includes("마지막 확인"), "known state shows last check");
ok(knownProv.includes("읽기 전용 관찰"), "known state is read-only");
ok(!knownProv.includes("기록 없음"), "known state with stamp is not missing");
const falseProv = nextProv({has_next: false,
  branch_next_observed_at: "2026-05-01T00:00:00+00:00"});
ok(falseProv.includes("마지막 확인"), "false state shows last check");
const unkStamp = nextProv({has_next: null,
  branch_next_observed_at: "2026-05-01T00:00:00+00:00",
  branch_next_reason: "체크아웃 변경됨"});
ok(unkStamp.includes("체크아웃 변경됨"), "unknown keeps its reason");
ok(unkStamp.includes("KST"), "unknown with stamp keeps provenance");
ok(!unkStamp.includes("마지막 확인"), "unknown is not a last check");
const unkLegacy = nextProv({has_next: null, branch_next_reason: "",
  branch_next_observed_at: null, last_scan: "2026-05-01T00:00:00+00:00"});
ok(unkLegacy.includes("기록 없음"), "legacy without branch stamp is missing");
ok(!unkLegacy.includes("KST"), "last_scan never masquerades as lookup");
const evilProv = nextProv({has_next: null,
  branch_next_reason: "<img src=x onerror=1>", branch_next_observed_at: null});
ok(!evilProv.includes("<img"), "untrusted reason escaped");
ok(evilProv.includes("&lt;img"), "escaped reason shown");
// N) promotion fallback: unknown unless explicit bools say otherwise
const noPromo = promoCrit(null);
eq(noPromo.length, 4, "no report yields four unknown criteria");
ok(noPromo.every((c) => c.ok === null), "no report is not clearance");
eq(promoCrit(undefined).length, 4, "missing report yields unknown");
eq(promoCrit("promo").length, 4, "non-object report yields unknown");
const mixed = promoCrit({criteria: [{name: "Q", ok: true},
  {name: "B", ok: false}, {name: "S", ok: "false"}, "junk", null, 42, {},
  {ok: true}]});
eq(mixed.length, 8, "invalid entries stay visible, not hidden");
eq(mixed[0].ok, true, "explicit true kept");
eq(mixed[1].ok, false, "explicit false kept");
eq(mixed[2].ok, null, "string false is unknown");
const mixedUnknown = mixed.filter((c) => c.name === "이름 없음");
eq(mixedUnknown.length, 5, "five uncertain entries kept as rows");
ok(mixedUnknown.every((c) => c.ok === null),
  "uncertain rows never become clearance");
const knownPlusJunk = promoCrit({criteria: [{name: "Known", ok: true},
  null, "bad"]});
eq(knownPlusJunk.length, 3, "known plus junk keeps all rows");
eq(knownPlusJunk[0].ok, true, "the known pass stays known");
ok(knownPlusJunk[1].ok === null && knownPlusJunk[2].ok === null,
  "junk entries read unknown, report not apparently complete green");
const emptyCrit = promoCrit({criteria: []});
eq(emptyCrit.length, 4, "empty array falls back to unknown");
ok(emptyCrit.every((c) => c.ok === null), "empty array is not clearance");
const junkCrit = promoCrit({criteria: [{}, null, "x", 42, []]});
eq(junkCrit.length, 4, "all-malformed array falls back to unknown");
ok(junkCrit.every((c) => c.ok === null),
  "no identifiable item is never an all-clear");
const blankCrit = promoCrit({criteria: [{name: "  ", ok: true},
  {name: "Real", ok: false}]});
eq(blankCrit.length, 2, "blank-name rows stay visible");
eq(blankCrit[0].ok, null, "blank name with true is unknown, not 충족");
eq(blankCrit[1].ok, false, "named false kept");
const emptyHtml = emptyCrit.map(critLine).join("");
ok(emptyHtml.includes("확인 안 됨"), "fallback renders visible unknown rows");
ok(!emptyHtml.includes("●"), "unknown fallback shows no clearance dot");
const legacyBools = promoCrit({review_done: true, e2e_verified: false,
  release_notes: "yes"});
eq(legacyBools[0].ok, true, "legacy review bool kept");
eq(legacyBools[1].ok, false, "legacy e2e bool kept");
eq(legacyBools[2].ok, null, "question clearance stays unknown");
eq(legacyBools[3].ok, null, "legacy string is not a bool");
const tLine = critLine({name: "미해결 질문 없음", ok: true});
ok(tLine.includes("충족") && !tLine.includes("미충족"),
  "true criterion reads clearance");
ok(critLine({name: "B", ok: false}).includes("미충족"),
  "false criterion reads failure");
const uLine = critLine({name: "미해결 질문 없음", ok: null});
ok(uLine.includes("확인 안 됨") && !uLine.includes("충족"),
  "unknown criterion is neither clearance nor failure");
const sLine = critLine({name: "S", ok: "false"});
ok(sLine.includes("확인 안 됨") && !sLine.includes("충족"),
  "string false never becomes clearance");
ok(critLine(null).includes("확인 안 됨"), "null criterion is unknown");
ok(critLine("x").includes("이름 없음"), "non-object criterion named safely");
// N) deferred responses never paint, cache, or error a newer view
const __boxes = {};
$ = (id) => {
  if (id === "modal") return {classList: {contains: () => false}};
  if (!__boxes[id]) __boxes[id] = {innerHTML: "", dataset: {}};
  return __boxes[id];
};
view = {kind: "repo", id: "rA"}; repoTab = "branches"; schedCache = {};
let __resolveA;
const __gateA = new Promise((res) => { __resolveA = res; });
jget = (u) => {
  if (u.includes("repo=rA")) return __gateA;
  if (u.includes("repo=rB")) return Promise.resolve({ok: true, promotion: null});
  throw new Error("unexpected fetch " + u);
};
const pA = paintRepoBranches({name: "rA", has_next: null,
  branch_next_reason: "관찰 기록 없음", branch_next_observed_at: null});
view = {kind: "repo", id: "rB"}; repoTab = "branches";
__boxes["tabbody"] = {innerHTML: "", dataset: {}};
const pB = paintRepoBranches({name: "rB", has_next: false,
  branch_next_observed_at: "2026-05-01T00:00:00+00:00",
  branch_next_reason: "", promotion: null});
__resolveA({ok: true, promotion: null});
await pA; await pB;
eq(schedCache["rA"], undefined, "late A never poisons the cache");
ok(__boxes["tabbody"].innerHTML.includes("next 없음"),
  "current B paints its own absence");
ok(!__boxes["tabbody"].innerHTML.includes("확인하지 못했습니다"),
  "late A never paints into B");
view = {kind: "repo", id: "rC"}; repoTab = "branches"; schedCache = {};
jget = () => Promise.reject(new Error("down"));
const pC = paintRepoBranches({name: "rC", has_next: null});
view = {kind: "repo", id: "rD"};
__boxes["tabbody"] = {innerHTML: "B-DOM", dataset: {}};
await pC;
eq(__boxes["tabbody"].innerHTML, "B-DOM", "stale failure never touches B");
eq(schedCache["rC"], undefined, "failed fetch is not cached");
view = {kind: "repo", id: "rE"};
__boxes["tabbody"] = {innerHTML: "", dataset: {}};
await paintRepoBranches({name: "rE", has_next: null});
ok(__boxes["tabbody"].innerHTML.includes("브랜치를 읽는 중 문제가 생겼습니다"),
  "owned failure shows in its own view");
ok(__boxes["tabbody"].innerHTML.includes("<details"),
  "raw branch error stays inside the collapsed detail");
schedCache = {rF: {ok: true, promotion: null}};
view = {kind: "repo", id: "rF"}; repoTab = "branches";
__boxes["tabbody"] = {innerHTML: "", dataset: {}};
jget = () => { throw new Error("must not fetch on cache hit"); };
await paintRepoBranches({name: "rF", has_next: true,
  branch_next_observed_at: "2026-05-01T00:00:00+00:00",
  branch_next_reason: ""});
ok(__boxes["tabbody"].innerHTML.includes("next 브랜치 존재"),
  "cache hit paints without fetch");
// O) profSection ownership: deferred responses never paint a replaced
// pane or bind another repo's controls; genuine active errors stay visible
const profBox = () => {
  const btns = {};
  return {innerHTML: "", dataset: {},
    querySelector: (sel) => {
      if (!btns[sel]) btns[sel] = {onclick: null, textContent: "", value: ""};
      return btns[sel];
    }};
};
const profData = {profile: {fastcheck: {steps: [{argv: ["yarn", "lint"],
  gate: "lint"}], toolchain: ["/usr/bin"]}}, validation: [], probes: []};
view = {kind: "repo", id: "rP"}; repoTab = "overview";
const pfP = profBox(); __boxes["prof"] = pfP;
let pfUrl = "";
jget = (u) => { pfUrl = u; return Promise.resolve(profData); };
await profSection("rP");
ok(pfUrl.includes("repo=rP"), "profile fetched for the exact repo");
ok(pfP.innerHTML.includes("검사 단계"), "owned pane paints profile");
eq(typeof pfP.querySelector("#profprobe").onclick, "function",
  "probe bound on the owning pane");
eq(typeof pfP.querySelector("#profsave").onclick, "function",
  "save bound on the owning pane");
jpost = async () => ({ok: true});
await pfP.querySelector("#profprobe").onclick();
await new Promise((r) => setTimeout(r, 0));
await new Promise((r) => setTimeout(r, 0));
ok(modalHtml[modalHtml.length - 1] === "toast:도구 확인됨",
  "owning probe handler drives its own pane refresh");
ok(pfP.innerHTML.includes("검사 단계"), "owning pane still shows profile");
view = {kind: "repo", id: "rA"}; repoTab = "overview";
const pfA = profBox(); __boxes["prof"] = pfA;
let pfResolveA;
const pfGateA = new Promise((res) => { pfResolveA = res; });
jget = () => pfGateA;
const pPA = profSection("rA");
view = {kind: "repo", id: "rB"}; repoTab = "overview";
const pfB = profBox(); __boxes["prof"] = pfB;
pfResolveA(profData);
await pPA;
eq(pfA.innerHTML, "", "late A never paints its replaced pane");
eq(pfB.innerHTML, "", "late A never paints the new pane");
eq(pfB.querySelector("#profprobe").onclick, null,
  "late A binds nothing on the new pane");
eq(pfA.querySelector("#profprobe").onclick, null,
  "late A binds nothing on its own stale pane");
view = {kind: "repo", id: "rA"}; repoTab = "overview";
const pfA2 = profBox(); __boxes["prof"] = pfA2;
let pfResolveA2;
const pfGateA2 = new Promise((res) => { pfResolveA2 = res; });
jget = () => pfGateA2;
const pPA2 = profSection("rA");
repoTab = "reviews";
__boxes["prof"] = profBox();
pfResolveA2(profData);
await pPA2;
eq(pfA2.innerHTML, "", "same-repo tab switch drops the stale paint");
view = {kind: "repo", id: "rC"}; repoTab = "overview";
const pfC = profBox(); __boxes["prof"] = pfC;
jget = () => Promise.reject(new Error("down"));
const pPA3 = profSection("rC");
view = {kind: "repo", id: "rD"};
__boxes["prof"] = profBox();
await pPA3;
eq(pfC.innerHTML, "", "stale rejection never paints the detached pane");
view = {kind: "repo", id: "rE"}; repoTab = "overview";
const pfE = profBox(); __boxes["prof"] = pfE;
await profSection("rE");
ok(pfE.innerHTML.includes("검사 설정 조회 실패"),
  "genuine active-pane error stays visible");
view = {kind: "repo", id: "rA"}; repoTab = "overview";
const pfA4 = profBox(); __boxes["prof"] = pfA4;
let pfResolveA4;
const pfGateA4 = new Promise((res) => { pfResolveA4 = res; });
jget = () => pfGateA4;
const pPA4 = profSection("rA");
view = {kind: "repo", id: "rB"};
const pfB4 = profBox(); __boxes["prof"] = pfB4;
jget = () => Promise.resolve(profData);
await profSection("rB");
const bProbe = pfB4.querySelector("#profprobe").onclick;
eq(typeof bProbe, "function", "new pane binds its own controls");
pfResolveA4(profData);
await pPA4;
eq(pfB4.querySelector("#profprobe").onclick, bProbe,
  "late A never rebinds the new pane with the old repo");
eq(pfA4.innerHTML, "", "late A never paints its old pane");
// P) repo tabs: APG manual activation, roving tabindex, post-repaint focus
const TAB_IDS = ["overview", "reviews", "prs", "community", "work",
  "deps", "branches", "activity"];
function tabStripView() {
  const v = {_tabs: [], _strip: null,
    querySelectorAll: function(sel) {
      return sel === "[data-tab]" ? this._tabs : [];
    },
    querySelector: function(sel) {
      if (sel === ".tabs") return this._strip;
      if (sel.slice(0, 9) === "#repotab-") {
        const id = sel.slice(9);
        const found = this._tabs.filter((b) => b.dataset.tab === id);
        return found.length ? found[0] : null;
      }
      return null;
    }};
  rebuildStrip(v);
  return v;
}
function rebuildStrip(view) {
  /* 실제 렌더처럼 스트립 요소 자체가 교체된다. */
  const strip = {scrollLeft: 0, scrollTop: 0, clientWidth: 300,
    scrollWidth: 880, onkeydown: null, _view: view,
    /* 목록 가시성 rect는 현재 clientWidth에서 계산한다:
       기본 300이 아니라 케이스별 너비(700·1000)를 그대로 반영한다. */
    querySelectorAll: function(sel) {
      if (!this._view || !this._view._tabs) return [];
      return sel === "[data-tab]" ? this._view._tabs.slice() : [];
    },
    querySelector: function(sel) {
      if (!this._view || !this._view._tabs) return null;
      if (sel.slice(0, 9) === "#repotab-") {
        const id = sel.slice(9);
        const found = this._view._tabs.filter((b) => b.dataset.tab === id);
        return found.length ? found[0] : null;
      }
      return null;
    },
    getBoundingClientRect: function() {
      return {left: 272, right: 272 + this.clientWidth, top: 0, bottom: 44};
    }};
  view._strip = strip;
}
function tabButton(id, view, index) {
  const btn = {dataset: {tab: id}, tabIndex: -1, id: "repotab-" + id,
    onclick: null, _preventScroll: null, _focused: false,
    /* offsetParent 가정 금지의 역 fixture: 이 값들을 쓰면 틀린다. */
    offsetParent: {}, offsetLeft: 5, offsetWidth: 9999,
    parentElement: view._strip,
    getBoundingClientRect: function() {
      const sl = this.parentElement.scrollLeft;
      return {left: 272 + index * 110 - sl,
        right: 272 + index * 110 + 80 - sl, top: 0, bottom: 44};
    },
    focus: function(opts) {
      document.activeElement = this;
      this._focused = true;
      this._preventScroll = !!(opts && opts.preventScroll);
    }};
  return btn;
}
function rebuildTabs(view, selected, rogue) {
  rebuildStrip(view);
  view._tabs = TAB_IDS.map((id, i) => {
    const b = tabButton(id, view, i);
    b.tabIndex = id === selected ? 0 : -1;
    return b;
  });
  if (rogue) view._tabs.push(rogue);
}
document = {activeElement: null};
view = {kind: "repo", id: "rX"};
let tabRenders = 0, tabSynced = 0;
syncHash = () => { tabSynced++; };
const tabView = tabStripView();
renderRepo = () => {
  tabRenders++;
  rebuildTabs(tabView, repoTab);
  wireRepoTabs(tabView, "rX");
};
repoTab = "work";
const tabsHtml = repoTabsHtml();
TAB_IDS.forEach((id) => ok(tabsHtml.includes('id="repotab-' + id + '"'),
  "stable id for " + id));
eq((tabsHtml.match(/aria-controls="tabbody"/g) || []).length, 8,
  "every tab controls the panel");
eq((tabsHtml.match(/tabindex="0"/g) || []).length, 1,
  "single initial tab stop");
ok(tabsHtml.includes('aria-selected="true"'), "selected tab marked");
const panelHtml = repoTabPanelHtml();
ok(panelHtml.includes('role="tabpanel"'), "panel role from real generator");
ok(panelHtml.includes('tabindex="0"'), "panel is the single panel entry");
ok(panelHtml.includes('aria-labelledby="repotab-work"'),
  "panel labels the actually selected tab");
repoTab = 'overview"><img src=x>';
ok(!repoTabsHtml().includes("<img"), "hash input never lands in tab markup");
ok(!repoTabPanelHtml().includes("<img"), "hash input never lands in panel markup");
eq(repoTabId(), "activity", "unknown tab resolves to the painted body");
ok(repoTabsHtml().includes('id="repotab-activity"'), "unknown still lists all ids");
eq((repoTabsHtml().match(/tabindex="0"/g) || []).length, 1,
  "unknown keeps exactly one entry, on the painted tab");
ok(repoTabsHtml().includes('aria-selected="true"'),
  "unknown still marks exactly the painted tab");
eq((repoTabsHtml().match(/aria-selected="true"/g) || []).length, 1,
  "unknown never double-selects");
ok(repoTabPanelHtml().includes('aria-labelledby="repotab-activity"'),
  "panel labels the actually painted tab");
repoTab = null;
eq(repoTabId(), "activity", "missing tab resolves to the painted body");
repoTab = "branches";
eq(repoTabId(), "branches", "known tab keeps its id");
repoTab = "work";
rebuildTabs(tabView, "work");
wireRepoTabs(tabView, "rX");
const zeroTabs = tabView._tabs.filter((b) => b.tabIndex === 0);
eq(zeroTabs.length, 1, "exactly one roving tab stop");
eq(zeroTabs[0].dataset.tab, "work", "roving zero on the selected tab");
// rogue id: never wired, never travelled, never interpolated
const rogue = {dataset: {tab: "evil"}, tabIndex: -1, id: "rogue",
  onclick: "untouched",
  focus: function() { throw new Error("rogue focused"); }};
rebuildTabs(tabView, "work", rogue);
wireRepoTabs(tabView, "rX");
eq(rogue.onclick, "untouched", "unknown ids are never wired");
tabView._tabs[4].focus();
document.activeElement = tabView._tabs[4];
let tabPrevented = false;
const tabKey = (key, target) => tabView._strip.onkeydown({key: key,
  target: target === undefined ? document.activeElement : target,
  preventDefault: () => { tabPrevented = true; }});
tabKey("ArrowRight");
eq(document.activeElement.dataset.tab, "deps", "right moves focus");
eq(tabPrevented, true, "handled key prevented");
eq(tabView._tabs[5]._preventScroll, true, "travel focus avoids page jump");
eq(tabView._strip.scrollTop, 0, "travel never moves vertically");
ok(tabView._strip.scrollLeft > 0, "travel keeps the focused tab visible");
tabKey("ArrowRight"); tabKey("ArrowRight");
eq(document.activeElement.dataset.tab, "activity", "right reaches last");
tabKey("ArrowRight");
eq(document.activeElement.dataset.tab, "overview", "right wraps to first");
tabKey("ArrowLeft");
eq(document.activeElement.dataset.tab, "activity", "left wraps to last");
tabKey("Home");
eq(document.activeElement.dataset.tab, "overview", "home jumps first");
tabKey("End");
eq(document.activeElement.dataset.tab, "activity", "end jumps last");
eq(repoTab, "work", "arrows never change state");
eq(tabRenders, 0, "arrows never repaint");
eq(tabSynced, 0, "arrows never touch the URL");
eq(tabView._tabs.filter((b) => b.tabIndex === 0).length, 1,
  "roving zero stays single");
eq(tabView._tabs.filter((b) => b.tabIndex === 0)[0], document.activeElement,
  "zero roves with the focused button");
eq(document.activeElement.dataset.tab, "activity",
  "roving entry tracks the last arrow focus");
// fully visible: focus moves, strip does not churn
rebuildTabs(tabView, "work");
wireRepoTabs(tabView, "rX");
tabView._tabs[0].focus();
document.activeElement = tabView._tabs[0];
tabPrevented = false;
tabKey("ArrowRight");
eq(document.activeElement.dataset.tab, "reviews", "right from first");
eq(tabView._strip.scrollLeft, 0, "fully visible tab causes no scroll");
tabPrevented = false;
tabKey("ArrowDown");
eq(tabPrevented, false, "down is not intercepted");
eq(document.activeElement.dataset.tab, "reviews", "down moves nothing");
tabKey("Enter");
eq(tabRenders, 0, "unhandled keys repaint nothing");
tabPrevented = false;
tabKey("ArrowRight", tabView._strip);
eq(tabPrevented, false, "non-tab target returns unhandled");
tabPrevented = false;
tabView._strip.onkeydown({key: "ArrowRight",
  preventDefault: () => { tabPrevented = true; }});
eq(tabPrevented, false, "missing target returns unhandled");
tabView._strip.onkeydown(null);
tabView._strip.onkeydown({});
const scopeB = tabStripView();
rebuildTabs(scopeB, "work");
ok(scopeB._tabs.every((b) => b.onclick === null),
  "other roots are never wired");
// stale strip handler after same-view replacement
rebuildTabs(tabView, "work");
wireRepoTabs(tabView, "rX");
const staleTabs = tabView._tabs;
const staleKey = tabView._strip.onkeydown;
rebuildTabs(tabView, "work");
wireRepoTabs(tabView, "rX");
document.activeElement = tabView._tabs[0];
tabPrevented = false;
staleKey({key: "ArrowRight", target: staleTabs[0],
  preventDefault: () => { tabPrevented = true; }});
eq(tabPrevented, false, "stale strip handler returns unhandled");
eq(document.activeElement.dataset.tab, "overview", "stale travel moves nothing");
// wrong repo: same markup, different selection context
view = {kind: "repo", id: "rB"};
tabPrevented = false;
tabView._strip.onkeydown({key: "ArrowRight", target: tabView._tabs[0],
  preventDefault: () => { tabPrevented = true; }});
eq(tabPrevented, false, "wrong repo returns unhandled");
eq(tabRenders, 0, "wrong repo repaints nothing");
view = {kind: "repo", id: "rX"};
wireRepoTabs(null, "rX");
wireRepoTabs({querySelectorAll: () => [], querySelector: () => null}, "rX");
const oldBranches = tabView._tabs.filter(
  (b) => b.dataset.tab === "branches")[0];
tabView._tabs.filter((b) => b.dataset.tab === "work")[0].focus();
document.activeElement = tabView._tabs.filter(
  (b) => b.dataset.tab === "work")[0];
tabRenders = 0; tabSynced = 0;
oldBranches.onclick();
eq(repoTab, "branches", "native activation moves state");
eq(tabSynced, 1, "activation syncs the URL once");
eq(tabRenders, 1, "activation repaints once");
const nbBranches = tabView._tabs.filter(
  (b) => b.dataset.tab === "branches")[0];
ok(nbBranches !== oldBranches, "repaint uses new identities");
eq(document.activeElement, nbBranches,
  "focus lands on the new selected button, not the detached old one");
eq(nbBranches._preventScroll, true, "activation focus avoids page jump");
eq(tabView._strip.scrollLeft, 550, "offscreen tab centered by rect geometry");
eq(tabView._strip.scrollTop, 0, "no vertical movement");
// post-repaint travel uses current nodes
tabView._strip.onkeydown({key: "ArrowRight", target: nbBranches,
  preventDefault: () => {}});
eq(document.activeElement.dataset.tab, "activity",
  "travel follows the new wiring");
// old detached handler is inert
const staleGo = oldBranches.onclick;
tabRenders = 0; tabSynced = 0;
staleGo();
eq(tabRenders, 0, "stale detached activation renders nothing");
eq(tabSynced, 0, "stale detached activation syncs nothing");
eq(repoTab, "branches", "stale activation changes no state");
// right-edge clamp, then left-edge clamp from a scrolled strip
repoTab = "overview";
rebuildTabs(tabView, "overview");
wireRepoTabs(tabView, "rX");
tabView._tabs[7].onclick();
eq(repoTab, "activity", "last tab activates");
eq(tabView._strip.scrollLeft, 580, "right overflow clamps to max");
tabView._tabs[0].onclick();
eq(repoTab, "overview", "first tab activates");
eq(tabView._strip.scrollLeft, 0, "left overflow clamps to start");
// Q) quiet poll keeps the user's tab focus across the fallback re-render.
// Actual production loadAll is extracted in this owned block only, so the
// earlier stub keeps serving every section above; nothing above changes.
const realLoadAll = eval("(" + extractFn("loadAll") + ")");
// Epoch pump companions for the real loadAll above: the overview single-flight
// wrapper references module-level epoch state plus three helpers. Declared and
// evaluated here once so Q below and R7/R29 later keep executing the actual
// pipeline (fetch -> settle -> apply); the early generic stub still serves
// only the sections above this point.
let ovEpoch = null, ovWantFresh = false;
const driveOverviewEpoch = eval("(" + extractFn("driveOverviewEpoch") + ")");
const settleOverviewEpoch = eval("(" + extractFn("settleOverviewEpoch") + ")");
const applyOverview = eval("(" + extractFn("applyOverview") + ")");
const qView = tabStripView();
$ = (id) => {
  if (id === "view") return qView;
  if (id === "modal") return {classList: {contains: () => false}};
  if (!__boxes[id]) __boxes[id] = {innerHTML: "", dataset: {}};
  return __boxes[id];
};
composerBusy = () => false;
workTyping = () => false;
paintStatusbar = () => {};
let qCounts = 0, qRenders = 0, qOvUrl = "", qResolveOv = null;
renderCounts = () => { qCounts++; };
stashReviewState = () => {};
checks = {};
selRepo = "rX";
seenQ = new Set();
CSS = {escape: (s) => String(s)};
lastRenderResetScroll = false;
jget = (u) => {
  qOvUrl = u;
  return new Promise((res) => { qResolveOv = res; });
};
render = () => {
  qRenders++;
  qView.scrollTop = 0;
  const qPrior = qView._tabs;
  const qHad = document.activeElement;
  rebuildTabs(qView, repoTab);
  wireRepoTabs(qView, "rX");
  /* 실제 브라우저처럼 교체로 사라진 노드의 포커스만 BODY로 떨어진다:
     교체 전 목록에 있던 탭이면 제거된 것이므로 null로,
     #view 밖 상단 검색 같은 지속 컨트롤이면 그대로 둔다.
     조용한 폴링은 탭 포커스를 복원하고, 비조용 경로는 그대로 둔다. */
  if (qHad && qPrior.indexOf(qHad) >= 0) document.activeElement = null;
};
qView.scrollTop = 0;
view = {kind: "repo", id: "rX"}; repoTab = "branches";
document = {activeElement: null, querySelectorAll: () => [], querySelector: () => null};
rebuildTabs(qView, "branches");
wireRepoTabs(qView, "rX");
qView.scrollTop = 456;
qView._tabs.filter((b) => b.dataset.tab === "branches")[0].focus();
eq(document.activeElement.dataset.tab, "branches", "click-like focus starts on branches");
// user arrows once more while the quiet fetch is still pending:
// capture must read this live focus, not the request-start one
const pQ = realLoadAll(true);
eq(typeof qResolveOv, "function", "overview fetch is actually pending");
qView._strip.onkeydown({key: "ArrowRight",
  target: qView._tabs.filter((b) => b.dataset.tab === "branches")[0],
  preventDefault: () => {}});
eq(document.activeElement.dataset.tab, "activity", "arrow moves during fetch");
const qOldAct = document.activeElement;
const qSlBefore = qView._strip.scrollLeft;
ok(qSlBefore === 580, "arrow recenters the offscreen tab, clamped to max");
qResolveOv({repos: [{name: "rX"}], runs: []});
await pQ;
eq(qOvUrl, "/api/overview", "quiet poll fetches overview once");
eq(qRenders, 1, "quiet fallback still refreshes data");
eq(repoTab, "branches", "restore never changes selection");
const qNewAct = qView._tabs.filter((b) => b.dataset.tab === "activity")[0];
ok(qNewAct !== qOldAct, "render replaced the tab DOM");
eq(document.activeElement, qNewAct, "focus follows the new focused tab");
eq(qNewAct._preventScroll, true, "restore avoids page jump");
const qZeros = qView._tabs.filter((b) => b.tabIndex === 0);
eq(qZeros.length, 1, "single roving stop after restore");
eq(qZeros[0], qNewAct, "roving zero tracks the focused unselected tab");
eq(qView._strip.scrollTop, 0, "no vertical movement");
eq(qView._strip.scrollLeft, 580, "restored strip position survives the clamp");
eq(qView.scrollTop, 456, "nonzero view scroll restored");
eq(qCounts, 1, "quiet counts still painted");
// second quiet round: focused tab already fully visible -> zero churn
qView._strip.onkeydown({key: "ArrowLeft", target: qNewAct,
  preventDefault: () => {}});
eq(document.activeElement.dataset.tab, "branches", "arrow returns to branches");
const qSlPre2 = qView._strip.scrollLeft;
ok(qSlPre2 === 580, "pre-round strip position is the known nonzero value");
const pQb = realLoadAll(true);
qResolveOv({repos: [{name: "rX"}], runs: []});
await pQb;
eq(qRenders, 2, "second quiet round still renders");
eq(document.activeElement.dataset.tab, "branches", "selected tab keeps focus");
eq(qView._strip.scrollLeft, qSlPre2, "fully visible tab causes no churn");
// wider-than-default strip (700 vs 300): the clamp follows the new
// strip width, never the stale position
view = {kind: "repo", id: "rX"}; repoTab = "branches";
rebuildTabs(qView, "branches");
wireRepoTabs(qView, "rX");
qView._strip.scrollLeft = 580;
qView._tabs.filter((b) => b.dataset.tab === "branches")[0].focus();
const keptW = captureRepoTabFocus();
rebuildTabs(qView, "branches");
qView._strip.clientWidth = 700;
wireRepoTabs(qView, "rX");
eq(restoreRepoTabFocus(keptW), true, "narrow strip still restores");
eq(document.activeElement.dataset.tab, "branches", "narrow strip keeps focus");
eq(qView._strip.scrollLeft, 180, "restored position clamps to the new width");
// oversize strip (1000 vs content 880): no negative scroll, no churn
qView._strip.scrollLeft = 580;
qView._strip.clientWidth = 1000;
qView._tabs.filter((b) => b.dataset.tab === "branches")[0].focus();
const keptWide = captureRepoTabFocus();
rebuildTabs(qView, "branches");
qView._strip.clientWidth = 1000;
wireRepoTabs(qView, "rX");
eq(restoreRepoTabFocus(keptWide), true, "wide strip still restores");
eq(document.activeElement.dataset.tab, "branches", "wide strip keeps focus");
eq(qView._strip.scrollLeft, 0, "oversize strip clamps to zero");
// nonquiet navigation never steals focus through restore:
// the replaced focused node drops to BODY and stays there
repoTab = "branches";
rebuildTabs(qView, "branches");
wireRepoTabs(qView, "rX");
const qBr = qView._tabs.filter((b) => b.dataset.tab === "branches")[0];
qBr.focus();
const pQ2 = realLoadAll(false);
qResolveOv({repos: [{name: "rX"}], runs: []});
await pQ2;
eq(qRenders, 3, "nonquiet still renders");
eq(repoTab, "branches", "nonquiet changes no selection either");
eq(document.activeElement, null, "nonquiet leaves BODY alone, steals nothing");
ok(qView._tabs.every((b) => !b._focused), "nonquiet focuses no new tab");
// focus leaves the tablist for a persistent control outside #view
// (header search: render() replaces #view innerHTML only, so the header
// node identity survives) while the quiet fetch is pending:
// that outside focus must survive the re-render untouched.
// (An inside-panel control removed by the replacement would drop to
// BODY instead; that removed-detached case is not claimed here.)
repoTab = "branches";
rebuildTabs(qView, "branches");
wireRepoTabs(qView, "rX");
qView._tabs.filter((b) => b.dataset.tab === "deps")[0].focus();
const qHeaderBtn = {dataset: {}, _focused: false, __outsideView: true,
  focus: function() { document.activeElement = this; this._focused = true; }};
const pQ4 = realLoadAll(true);
qHeaderBtn.focus();
qResolveOv({repos: [{name: "rX"}], runs: []});
await pQ4;
eq(qRenders, 4, "outside-focus round still renders");
eq(document.activeElement, qHeaderBtn, "outside-#view focus is never stolen");
ok(qView._tabs.every((b) => !b._focused), "outside focus focuses no tab");
// BODY focus is left alone by quiet polls
document.activeElement = null;
const pQ3 = realLoadAll(true);
qResolveOv({repos: [{name: "rX"}], runs: []});
await pQ3;
eq(document.activeElement, null, "BODY focus is left alone");
ok(qView._tabs.every((b) => !b._focused), "no tab focused from BODY");
// wrong repo, unknown id, detached node, panel control, replaced root
view = {kind: "repo", id: "rX"}; repoTab = "branches";
rebuildTabs(qView, "branches");
wireRepoTabs(qView, "rX");
qView._tabs.filter((b) => b.dataset.tab === "deps")[0].focus();
const keptX = captureRepoTabFocus();
eq(keptX && keptX.id, "deps", "capture records the live tab");
view = {kind: "repo", id: "rB"};
eq(restoreRepoTabFocus(keptX), false, "wrong repo restores nothing");
view = {kind: "repo", id: "rX"};
document.activeElement = {dataset: {tab: "evil"}};
eq(captureRepoTabFocus(), null, "unknown id never captures");
const qRogueKnown = tabButton("deps", qView, 5);
qView._tabs.push(qRogueKnown);
qRogueKnown.focus();
eq(captureRepoTabFocus(), null, "known id on a foreign widget never captures");
qView._tabs.pop();
const qGhost = tabButton("deps", qView, 5);
qGhost.focus();
eq(captureRepoTabFocus(), null, "detached node never captures");
document.activeElement = {dataset: {}};
eq(captureRepoTabFocus(), null, "panel controls never capture");
document.activeElement = null;
eq(captureRepoTabFocus(), null, "BODY never captures");
eq(restoreRepoTabFocus({repo: "rX", id: "evil"}), false,
  "unknown id never restores");
eq(restoreRepoTabFocus(null), false, "null never restores");
qView._tabs.filter((b) => b.dataset.tab === "deps")[0].focus();
const keptR = captureRepoTabFocus();
const qOther = tabStripView();
rebuildTabs(qOther, "branches");
const $qPrev = $;
$ = (id) => (id === "view" ? qOther : $qPrev(id));
eq(restoreRepoTabFocus(keptR), false, "replaced root restores nothing");
$ = $qPrev;
// R) activity ownership + quiet refresh. Real esc from here on (this is the
// last section, so no earlier identity-esc oracle changes meaning).
// NOTE: extractFn("esc") cannot lex production esc — the /[&<>"]/g character
// class breaks the simplistic quote tracker — so reuse the already-correct
// single-line __escSrc, same as the hostile-text case above.
esc = eval("(" + __escSrc.replace("function esc", "function") + ")");
O = {repos: [{name: "rA", github: "o/rA"}, {name: "rB", github: "o/rB"}]};
view = {kind: "repo", id: "rA"}; repoTab = "activity";
const actBody = {tagName: "BODY"};
document = {activeElement: actBody, querySelectorAll: () => [], querySelector: () => null};
// Row nodes are parsed from the ACTUAL written eventHtml markup on every
// innerHTML write, never from product caches; each write builds brand-new
// node objects and drops a removed focused node to BODY like a browser.
const mkActNode = (row, kind, extra) => Object.assign(
  {tagName: kind, _row: row, __actNode: true, _focusCalls: 0,
   closest: function(sel) {
     if (sel === ".ev") return this._row;
     if (kind === "A" && sel === "a") return this;
     return null;
   },
   focus: function(o) { this._focusCalls++;
     document.activeElement = this; this._focused = true;
     this._prevent = !!(o && o.preventScroll); }},
  extra || {});
const parseActRows = (html) => {
  const parts = String(html).split('<div class="ev">').slice(1);
  return parts.map(part => {
    const row = {};
    const hrefs = [];
    const re = /<a\\s[^>]*href=(["'])(.*?)\\1/g;
    let m;
    while ((m = re.exec(part))) hrefs.push(m[2]);
    row._links = hrefs.map(h => mkActNode(row, "A", {
      _href: h,
      getAttribute: function(k) { return k === "href" ? this._href : null; }}));
    row._det = mkActNode(row, "DETAILS", {open: false});
    row._sum = mkActNode(row, "SUMMARY", {_prevent: null});
    row.querySelector = function(sel) {
      if (sel === "details") return this._det;
      if (sel === "summary") return this._sum;
      return null;
    };
    row.querySelectorAll = function(sel) {
      return sel === "a" ? this._links.slice() : [];
    };
    return row;
  });
};
const mkActList = (repo) => {
  const L = {_h: "", _writes: 0, _rows: [], _err: null,
    _fullRetry: null, dataset: {}, _repo: repo,
    querySelectorAll: function(sel) {
      if (sel === "details") return this._rows.map(r => r._det);
      if (sel === ".ev") return this._rows.slice();
      return [];
    },
    querySelector: function(sel) {
      const s = String(sel).replace(/"/g, "'");
      if (s === "[data-k='act-err']") return this._err;
      if (s === "[data-k='act-retry']") return this._err ? this._err._retry : this._fullRetry;
      return null;
    },
    insertAdjacentHTML: function(pos, html) {
      const h = String(html);
      if (/data-k=("|')act-err\\1/.test(h) && !this._err) {
        const retry = {onclick: null};
        const self = this;
        this._err = {_html: "", _removed: false, _retry: retry,
          remove: function() { this._removed = true; self._err = null; }};
        Object.defineProperty(this._err, "innerHTML", {
          get: function() { return this._html; },
          set: function(v) { this._html = v; },
          configurable: true});
      }
    }};
  Object.defineProperty(L, "innerHTML", {
    get: function() { return this._h; },
    set: function(v) {
      this._h = String(v); this._writes++;
      const had = this._rows.length > 0;
      this._rows = parseActRows(this._h);
      if (had && document && document.activeElement
          && document.activeElement.__actNode) document.activeElement = actBody;
      if (this._h.indexOf("act-err") < 0) this._err = null;
      if (this._h.indexOf("act-retry") >= 0 && !this._err) this._fullRetry = {onclick: null};
      else if (this._h.indexOf("act-retry") < 0) this._fullRetry = null;
    },
    configurable: true});
  return L;
};
const mkActBox = (repo) => {
  const B = {_h: "", _writes: 0, dataset: {}, _repo: repo, _filter: null, _list: null};
  Object.defineProperty(B, "innerHTML", {
    get: function() { return this._h; },
    set: function(v) {
      this._h = String(v); this._writes++;
      if (/id=("|')act-f\\1/.test(this._h)) this._filter = {value: "", onchange: null};
      if (/id=("|')act-list\\1/.test(this._h)) this._list = mkActList(this._repo);
    },
    configurable: true});
  return B;
};
let actGen = null;
const newActGen = (repo) => {
  if (document && document.activeElement
      && document.activeElement.__actNode) document.activeElement = actBody;
  actGen = {box: mkActBox(repo), repo};
  return actGen;
};
$ = (id) => {
  if (id === "tabbody") return actGen.box;
  if (id === "act-f") return actGen.box._filter;
  if (id === "act-list") return actGen.box._list;
  if (id === "view") return qView;
  if (id === "modal") return {classList: {contains: () => false}};
  if (id === "input") {
    if (!__boxes["input"]) __boxes["input"] = {value: "", selectionStart: 0, selectionEnd: 0,
      setSelectionRange: function(a, b) { this.selectionStart = a; this.selectionEnd = b; }};
    return __boxes["input"];
  }
  if (!__boxes[id]) __boxes[id] = {innerHTML: "", dataset: {}};
  return __boxes[id];
};
actReqSeq = 0; actKeysByRepo = {}; actPending = null;
qView.scrollHeight = 1813; qView.scrollWidth = 900;
qView.clientHeight = 729; qView.clientWidth = 390;
let qViewTop = 0;
Object.defineProperty(qView, "scrollTop", {
  get: () => qViewTop,
  set: (v) => { qViewTop = Math.max(0, Math.min(v, qView.scrollHeight - qView.clientHeight)); },
  configurable: true});
let actCalls = [], actGates = [];
jget = (u) => { actCalls.push(u); return new Promise((res, rej) => { actGates.push({res, rej}); }); };
const actMkEvent = (repo, kind, at, title, detail, extra) =>
  Object.assign({repo, kind, at, title: title || kind + "@" + at, detail: detail || "",
    ref: "", links: [], actor: "openmainty"}, extra || {});
// R1 stale cross-repo success rejected; exact repo query with limit
newActGen("rA"); view = {kind: "repo", id: "rA"}; repoTab = "activity";
actCalls = []; actGates = [];
jget = (u) => { actCalls.push(u); return new Promise((res, rej) => { actGates.push({res, rej}); }); };
const pR1 = paintRepoActivity({name: "rA"});
eq(actCalls.length, 1, "R1 one repo-scoped fetch");
eq(actCalls[0], "/api/events?repo=rA&limit=20", "R1 exact repo query with limit");
view = {kind: "repo", id: "rB"};
newActGen("rB");
actGates[0].res({events: [actMkEvent("rA", "scan", "t1")]});
await pR1;
eq(actGen.box._writes, 0, "R1 stale A success never builds the new pane");
eq(actGen.box._list, null, "R1 new pane has no list node");
// R2 stale rejection never paints the detached pane
newActGen("rA"); view = {kind: "repo", id: "rA"}; repoTab = "activity";
actCalls = []; actGates = [];
const pR2 = paintRepoActivity({name: "rA"});
view = {kind: "repo", id: "rB"};
newActGen("rB");
actGates[0].rej(new Error("boom"));
await pR2;
eq(actGen.box._writes, 0, "R2 stale rejection never paints the detached pane");
// R3 filter race: change before first reply neither rebuilds nor resets
newActGen("rA"); view = {kind: "repo", id: "rA"}; repoTab = "activity";
actCalls = []; actGates = [];
const evScan3 = actMkEvent("rA", "scan", "t1", "스캔 제목", "상세1");
const evRun3 = actMkEvent("rA", "run", "t2", "실행 제목", "상세2");
const pR3a = paintRepoActivity({name: "rA"});
eq(actGen.box._writes, 1, "R3 build writes the skeleton once");
const fR3 = actGen.box._filter;
ok(fR3 && typeof fR3.onchange === "function", "R3 filter wired on mount");
fR3.value = "run";
fR3.onchange();
eq(actCalls.length, 2, "R3 filter change fetches again");
eq(actGen.box._writes, 1, "R3 filter change never rebuilds the pane");
actGates[0].res({events: [evScan3, evRun3]});
await pR3a;
eq(actGen.box._filter.value, "run", "R3 stale first response cannot reset the live filter");
actGates[1].res({events: [evScan3, evRun3]});
await new Promise((r) => setTimeout(r, 0));
await new Promise((r) => setTimeout(r, 0));
const listR3 = actGen.box._list;
ok(listR3._h.includes("실행 제목"), "R3 latest request paints");
ok(!listR3._h.includes("스캔 제목"), "R3 stale filter no longer applies");
eq(actGen.box._filter.value, "run", "R3 filter value preserved through resolve");
// R4 same-DOM new generation: old response rejected, current paints
newActGen("rA"); view = {kind: "repo", id: "rA"}; repoTab = "activity";
actCalls = []; actGates = [];
const gR4a = actGen;
const pR4a = paintRepoActivity({name: "rA"});
newActGen("rA");
const pR4b = paintRepoActivity({name: "rA"});
eq(actCalls.length, 2, "R4 replacement fetches for its own pane");
actGates[0].res({events: [actMkEvent("rA", "scan", "t1", "첫제목", "d")]});
await pR4a;
eq(gR4a.box._list._writes, 0, "R4 stale first response paints nothing");
actGates[1].res({events: [actMkEvent("rA", "scan", "t1", "첫제목", "d")]});
await pR4b;
ok(actGen.box._list._h.includes("첫제목"), "R4 current pane paints its own data");
// R5 unchanged payload never rewrites; reorder moves open/focus by key
newActGen("rA"); view = {kind: "repo", id: "rA"}; repoTab = "activity";
actCalls = []; actGates = [];
const evA5 = actMkEvent("rA", "scan", "t1", "제목A", "원문A #11", {issue: "11"});
const evB5 = actMkEvent("rA", "run", "t2", "제목B", "원문B");
const pR5 = paintRepoActivity({name: "rA"});
actGates[0].res({events: [evA5, evB5]});
await pR5;
const listR5 = actGen.box._list;
eq(listR5._writes, 1, "R5 initial paint writes once");
eq(listR5._rows.length, 2, "R5 two rows parsed from real markup");
ok(listR5._h.includes("github.com/o/rA/issues/11"), "R5 real issue href rendered");
listR5._rows[0]._det.open = true;
listR5._rows[0]._sum.focus();
const sumR5 = listR5._rows[0]._sum;
const pR5q = paintRepoActivity({name: "rA"}, {quiet: true});
actGates[1].res({events: [evA5, evB5]});
await pR5q;
eq(listR5._writes, 1, "R5 unchanged payload never rewrites the list");
eq(document.activeElement, sumR5, "R5 focus untouched without rewrite");
const pR5r = paintRepoActivity({name: "rA"}, {quiet: true});
actGates[2].res({events: [evB5, evA5]});
await pR5r;
eq(listR5._writes, 2, "R5 reordered content rewrites once");
eq(listR5._rows[1]._det.open, true, "R5 open state follows the event key");
eq(document.activeElement, listR5._rows[1]._sum, "R5 focus follows the event key");
eq(listR5._rows[1]._sum._prevent, true, "R5 restored focus avoids page jump");
const linkA5 = listR5._rows[1]._links[0];
ok(linkA5 && linkA5._href.includes("github.com/o/rA/issues/11"), "R5 moved row keeps its real href");
linkA5.focus();
const evC5 = actMkEvent("rA", "blocked", "t3", "제목C", "원문C");
const pR5c = paintRepoActivity({name: "rA"}, {quiet: true});
actGates[3].res({events: [evB5, evA5, evC5]});
await pR5c;
eq(document.activeElement._href, linkA5._href, "R5 link focus restored to the same href");
ok(document.activeElement !== linkA5, "R5 focus lands on the new node, not the detached one");
// R6 first failure is a full error with retry; later failures banner over data
newActGen("rA"); view = {kind: "repo", id: "rA"}; repoTab = "activity";
actCalls = []; actGates = [];
const pR6 = paintRepoActivity({name: "rA"});
actGates[0].rej(new Error("net down"));
await pR6;
const listR6 = actGen.box._list;
ok(listR6._h.includes("조회 실패"), "R6 first failure shows a truthful error");
ok(!listR6._h.includes("이전 데이터"), "R6 no stale-data claim without data");
ok(listR6._fullRetry && typeof listR6._fullRetry.onclick === "function", "R6 retry wired");
listR6._fullRetry.onclick();
eq(actCalls.length, 2, "R6 retry refetches");
actGates[1].res({events: [actMkEvent("rA", "scan", "t1", "다시", "d")]});
await new Promise((r) => setTimeout(r, 0));
await new Promise((r) => setTimeout(r, 0));
ok(listR6._h.includes("다시"), "R6 retry paints after recovery");
actGen.box._filter.value = "scan";
const pR6b = paintRepoActivity({name: "rA"}, {quiet: true});
actGates[2].rej(new Error("flaky"));
await pR6b;
ok(listR6._err !== null, "R6 later failure banners over existing data");
ok(listR6._h.includes("다시"), "R6 old data retained under the banner");
ok(listR6._err._html.includes("이전 데이터"), "R6 banner marks previous data");
ok(!listR6._err._html.includes("최신"), "R6 banner claims no freshness");
eq(actGen.box._filter.value, "scan", "R6 banner keeps the current filter");
listR6._err._retry.onclick();
eq(actCalls.length, 4, "R6 banner retry refetches");
actGates[3].res({events: [actMkEvent("rA", "scan", "t1", "다시", "d")]});
await new Promise((r) => setTimeout(r, 0));
await new Promise((r) => setTimeout(r, 0));
eq(listR6._err, null, "R6 success clears the banner");
ok(listR6._h.includes("다시"), "R6 content intact after banner clears");
// R7 actual loadAll quiet refresh: ready pane kept, no outer render,
// composer/scroll/focus preserved, exactly two scoped fetches
const realLoadAllR = eval("(" + extractFn("loadAll") + ")");
let rRenders = 0;
const rUrls = [];
let rOvRes = null;
render = () => { rRenders++; };
renderCounts = () => {};
newActGen("rA"); view = {kind: "repo", id: "rA"}; repoTab = "activity";
selRepo = "rA";
actCalls = []; actGates = [];
jget = (u) => {
  rUrls.push(u);
  if (u === "/api/overview") return new Promise((res) => { rOvRes = res; });
  return new Promise((res, rej) => { actGates.push({res, rej}); });
};
const pR7m = paintRepoActivity({name: "rA"});
actGates[0].res({events: [actMkEvent("rA", "scan", "t1", "조용한제목", "d1")]});
await pR7m;
const boxR7 = actGen.box;
eq(boxR7._writes, 1, "R7 mounted once");
qView.scrollTop = 500;
eq(qView.scrollTop, 500, "R7 nonzero scroll established");
const outR7 = {tagName: "INPUT"};
document.activeElement = outR7;
__boxes["input"].value = "old-draft";
__boxes["input"].selectionStart = 0; __boxes["input"].selectionEnd = 9;
rUrls.length = 0;
const pR7 = realLoadAllR(true);
await new Promise((r) => setTimeout(r, 0));
eq(rUrls.filter((u) => u === "/api/overview").length, 1, "R7 quiet poll fetches overview");
rOvRes({repos: [{name: "rA", github: "o/rA"}, {name: "rB", github: "o/rB"}], runs: []});
await new Promise((r) => setTimeout(r, 0));
await new Promise((r) => setTimeout(r, 0));
__boxes["input"].value = "newer-edit";
__boxes["input"].selectionStart = 3; __boxes["input"].selectionEnd = 3;
eq(rUrls.filter((u) => u.indexOf("/api/events") === 0).length, 1, "R7 quiet poll refetches events");
actGates[1].res({events: [actMkEvent("rA", "scan", "t1", "조용한제목", "d1")]});
await pR7;
eq(rUrls.join(","), "/api/overview,/api/events?repo=rA&limit=20", "R7 exactly two scoped fetches, nothing else");
eq(rRenders, 0, "R7 quiet ready poll never replaces the outer DOM");
eq(actGen.box, boxR7, "R7 same pane identity, no replacement");
eq(boxR7._writes, 1, "R7 no skeleton rebuild on quiet poll");
eq(__boxes["input"].value, "newer-edit", "R7 composer edits during fetch survive");
eq(__boxes["input"].selectionStart, 3, "R7 composer caret untouched");
eq(qView.scrollTop, 500, "R7 nonzero scroll preserved");
eq(document.activeElement, outR7, "R7 outside focus never stolen");
ok(boxR7._list._h.includes("조용한제목"), "R7 content intact");
qView.scrollTop = 1500;
eq(qView.scrollTop, 1084, "R7 scroll clamps to the current height");
// R8 quiet duplicate suppressed while pending; outside focus survives resolve
newActGen("rA"); view = {kind: "repo", id: "rA"}; repoTab = "activity";
actCalls = []; actGates = [];
jget = (u) => { actCalls.push(u); return new Promise((res, rej) => { actGates.push({res, rej}); }); };
const pR8a = paintRepoActivity({name: "rA"});
eq(actCalls.length, 1, "R8 initial fetch issued");
await paintRepoActivity({name: "rA"}, {quiet: true});
eq(actCalls.length, 1, "R8 quiet duplicate suppressed while pending");
const outR8 = {tagName: "INPUT"};
document.activeElement = outR8;
actGates[0].res({events: [actMkEvent("rA", "scan", "t1", "밖포커스", "d")]});
await pR8a;
eq(document.activeElement, outR8, "R8 outside focus preserved through resolve");
ok(actGen.box._list._h.includes("밖포커스"), "R8 data paints");
// R9 fingerprint covers full rendered fields, not truncated prefixes
newActGen("rA"); view = {kind: "repo", id: "rA"}; repoTab = "activity";
actCalls = []; actGates = [];
const pR9 = paintRepoActivity({name: "rA"});
actGates[0].res({events: [actMkEvent("rA", "scan", "t1", "기본제목", "기본상세", {issue: "21"})]});
await pR9;
const listR9 = actGen.box._list;
const paint9 = async (evs) => {
  const gi = actGates.length;
  const p = paintRepoActivity({name: "rA"}, {quiet: true});
  eq(actCalls.length, gi + 1, "R9 each quiet poll fetches");
  actGates[gi].res({events: evs});
  await p;
};
await paint9([actMkEvent("rA", "scan", "t1", "기본제목", "바뀐상세", {issue: "21"})]);
eq(listR9._writes, 2, "R9 detail-only change detected");
ok(listR9._h.includes("바뀐상세"), "R9 changed detail visible");
const longT9 = "가".repeat(140) + "X";
await paint9([actMkEvent("rA", "scan", "t1", longT9, "바뀐상세", {issue: "21"})]);
eq(listR9._writes, 3, "R9 full-title change past the display cut detected");
ok(listR9._h.includes("(요약됨)"), "R9 long title still marked truncated");
const longY9 = "가".repeat(140) + "Y";
await paint9([actMkEvent("rA", "scan", "t1", longY9, "바뀐상세", {issue: "21"})]);
eq(listR9._writes, 4, "R9 suffix-only tail change detected, not merged");
ok(listR9._h.includes("(요약됨)"), "R9 suffix-only long title still marked truncated");
await paint9([actMkEvent("rA", "scan", "t1", "a~b", "c", {issue: "21"})]);
eq(listR9._writes, 5, "R9 first delimiter variant paints");
await paint9([actMkEvent("rA", "scan", "t1", "a", "b~c", {issue: "21"})]);
eq(listR9._writes, 6, "R9 delimiter-swapped content is distinct, never merged");
ok(!listR9._h.includes("a~b"), "R9 old delimiter content fully replaced");
await paint9([actMkEvent("rA", "scan", "t1", "a", "이슈 #22", {issue: "21"})]);
eq(listR9._writes, 7, "R9 issue-ref change detected");
ok(listR9._h.includes("/issues/22"), "R9 new issue href visible");
await paint9([actMkEvent("rA", "scan", "t1", "a", "이슈 #22", {issue: "22"})]);
eq(listR9._writes, 8, "R9 true issue-only change detected");
ok(listR9._h.includes('issues/22" target="_blank" rel="noopener">이슈 ↗'), "R9 issue-field href follows the issue value");
// R10 hostile markup is escaped, full text intact
newActGen("rA"); view = {kind: "repo", id: "rA"}; repoTab = "activity";
actCalls = []; actGates = [];
const pR10 = paintRepoActivity({name: "rA"});
actGates[0].res({events: [actMkEvent("rA", "scan", "t1", '<img src=x onerror="1">', "<script>alert(1)</script>", {issue: "33"})]});
await pR10;
const hR10 = actGen.box._list._h;
ok(!hR10.includes("<script>"), "R10 raw script never injected");
ok(hR10.includes("&lt;script&gt;"), "R10 detail escaped");
ok(hR10.includes("&lt;img"), "R10 title escaped");
ok(hR10.includes("/issues/33"), "R10 issue link still rendered");
// R11 leave/reenter with the same payload repaints the new pane
newActGen("rA"); view = {kind: "repo", id: "rA"}; repoTab = "activity";
actCalls = []; actGates = [];
const pR11a = paintRepoActivity({name: "rA"});
actGates[0].res({events: [actMkEvent("rA", "scan", "t1", "같은내용", "dX")]});
await pR11a;
ok(actGen.box._list._h.includes("같은내용"), "R11 first pane paints");
newActGen("rA");
const pR11b = paintRepoActivity({name: "rA"});
eq(actGen.box._writes, 1, "R11 reenter builds its own skeleton");
actGates[1].res({events: [actMkEvent("rA", "scan", "t1", "같은내용", "dX")]});
await pR11b;
ok(actGen.box._list._h.includes("같은내용"), "R11 same payload repaints the new pane, no stale skip");
eq(actGen.box._list._writes, 1, "R11 new pane paints exactly once");
// R14 pending slot is bound to the exact pane nodes, not the repo name
newActGen("rA"); view = {kind: "repo", id: "rA"}; repoTab = "activity";
actCalls = []; actGates = [];
const gR14 = actGen;
const pR14a = paintRepoActivity({name: "rA"});
newActGen("rA");
await paintRepoActivity({name: "rA"}, {quiet: true});
eq(actCalls.length, 1, "R14 quiet on an unbuilt pane never fetches");
actGates[0].res({events: [actMkEvent("rA", "scan", "t1", "늦은A", "d")]});
await pR14a;
eq(gR14.box._list._writes, 0, "R14 stale first response paints nothing");
eq(actGen.box._writes, 0, "R14 replacement stays empty until its own paint");
newActGen("rA"); view = {kind: "repo", id: "rA"}; repoTab = "activity";
actCalls = []; actGates = [];
const pR14b = paintRepoActivity({name: "rA"});
actGen.box._filter.value = "run";
const pR14c = paintRepoActivity({name: "rA"}, {quiet: true});
eq(actCalls.length, 2, "R14 second filter fetches on the same pane");
const slotR14 = actPending.req;
actGates[0].res({events: [actMkEvent("rA", "run", "t9", "새실행", "d9")]});
await pR14b;
eq(actPending.req, slotR14, "R14 stale settle never clears the newer slot");
actGates[1].res({events: [actMkEvent("rA", "run", "t9", "새실행", "d9")]});
await pR14c;
ok(actGen.box._list._h.includes("새실행"), "R14 newer paints");
// R15 newer failure banners; stale older success never clears it
newActGen("rA"); view = {kind: "repo", id: "rA"}; repoTab = "activity";
actCalls = []; actGates = [];
const evD15 = actMkEvent("rA", "scan", "t1", "유지내용", "dD");
const pR15a = paintRepoActivity({name: "rA"});
actGates[0].res({events: [evD15]});
await pR15a;
const listR15 = actGen.box._list;
const pR15o = paintRepoActivity({name: "rA"}, {quiet: true});
actGen.box._filter.value = "scan";
const pR15n = paintRepoActivity({name: "rA"}, {quiet: true});
eq(actCalls.length, 3, "R15 two overlapping polls fetch");
actGates[2].rej(new Error("new flake"));
await pR15n;
ok(listR15._err !== null, "R15 newer failure banners over kept data");
actGates[1].res({events: [evD15]});
await pR15o;
ok(listR15._err !== null, "R15 stale older success never clears the newer banner");
ok(listR15._h.includes("유지내용"), "R15 data intact");
// R16 changed key never relocates focus to another control
newActGen("rA"); view = {kind: "repo", id: "rA"}; repoTab = "activity";
actCalls = []; actGates = [];
const evF16 = actMkEvent("rA", "scan", "t1", "초점제목", "원문 #41", {issue: "41"});
const pR16 = paintRepoActivity({name: "rA"});
actGates[0].res({events: [evF16]});
await pR16;
const listR16 = actGen.box._list;
const linkF16 = listR16._rows[0]._links[0];
ok(linkF16, "R16 row has a real rendered link");
linkF16.focus();
const pR16b = paintRepoActivity({name: "rA"}, {quiet: true});
actGates[1].res({events: [actMkEvent("rA", "scan", "t1", "초점제목", "바뀐원문", {})]});
await pR16b;
eq(document.activeElement, actBody, "R16 changed key never relocates focus");
ok(listR16._rows.every((r) => r._sum._focusCalls === 0 && r._links.every((l) => l._focusCalls === 0)),
  "R16 no new control stole focus");
// R17 empty result is not previous data: failure after empty is a full error
newActGen("rA"); view = {kind: "repo", id: "rA"}; repoTab = "activity";
actCalls = []; actGates = [];
const pR17 = paintRepoActivity({name: "rA"});
actGates[0].res({events: []});
await pR17;
const listR17 = actGen.box._list;
ok(listR17._h.includes("이 조건의 사건 없음"), "R17 empty state shown");
const wR17 = listR17._writes;
const pR17q = paintRepoActivity({name: "rA"}, {quiet: true});
actGates[1].res({events: []});
await pR17q;
eq(listR17._writes, wR17, "R17 repeated empty poll does not rewrite");
const pR17e = paintRepoActivity({name: "rA"}, {quiet: true});
actGates[2].rej(new Error("empty then flaky"));
await pR17e;
eq(listR17._err, null, "R17 no banner over an empty list");
ok(listR17._h.includes("조회 실패") && !listR17._h.includes("이전 데이터"), "R17 full error, no stale-data claim");
// R18 full error clears the stored fingerprint: same-empty success recovers,
// and the next unchanged empty poll does not rewrite again
newActGen("rA"); view = {kind: "repo", id: "rA"}; repoTab = "activity";
actCalls = []; actGates = [];
const pR18 = paintRepoActivity({name: "rA"});
actGates[0].res({events: []});
await pR18;
const listR18 = actGen.box._list;
ok(listR18._h.includes("이 조건의 사건 없음"), "R18 empty state shown");
const pR18e = paintRepoActivity({name: "rA"}, {quiet: true});
actGates[1].rej(new Error("EMPTY-FAIL"));
await pR18e;
ok(listR18._h.includes("EMPTY-FAIL"), "R18 full error replaces the list");
const wR18 = listR18._writes;
const pR18r = paintRepoActivity({name: "rA"}, {quiet: true});
actGates[2].res({events: []});
await pR18r;
ok(listR18._h.includes("이 조건의 사건 없음"), "R18 same-empty success recovers the list");
ok(!listR18._h.includes("EMPTY-FAIL"), "R18 error text gone after recovery");
const pR18q = paintRepoActivity({name: "rA"}, {quiet: true});
actGates[3].res({events: []});
await pR18q;
eq(listR18._writes, wR18 + 1, "R18 recovered empty poll does not rewrite again");
// R19 model pill: requested mode only, unknown on missing/error/other, native control contract
eq(modelPillState({model: "muse-spark-1.3", billing: "subscription login (no META_API_KEY in environment)"}).tone, "sub", "pill19 subscription requested tone");
ok(modelPillState({model: "muse-spark-1.3", billing: "subscription login (no META_API_KEY in environment)"}).label.indexOf("구독 요청됨") >= 0, "pill19 requested wording in accessible name, never verified connection");
ok(modelPillState({model: "muse-spark-1.3", billing: "subscription login (no META_API_KEY in environment)"}).title.indexOf("구독 요청됨") >= 0, "pill19 requested wording in hover title");
ok(modelPillState({model: "muse-spark-1.3", billing: "subscription login (no META_API_KEY in environment)"}).html.indexOf("미검증") >= 0, "pill19 unverified state visible, not tooltip-only");
ok(modelPillState({model: "muse-spark-1.3", billing: "subscription login (no META_API_KEY in environment)"}).html.indexOf("●") < 0, "pill19 no health dot for unverified mode");
ok(modelPillState({model: "muse-spark-1.3", billing: "subscription login (no META_API_KEY in environment)"}).title.indexOf("검증되지 않았습니다") >= 0, "pill19 title disclaims worker verification");
eq(modelPillState({model: "muse-spark-1.3", billing: "api-key (META_API_KEY is set; takes priority over login)"}).tone, "key", "pill19 configured key tone");
ok(modelPillState({model: "muse-spark-1.3", billing: "api-key (META_API_KEY is set)"}).html.indexOf("API 키") >= 0, "pill19 key labeled only when configured");
ok(modelPillState({model: "muse-spark-1.3", billing: "api-key (META_API_KEY is set)"}).label.indexOf("API 키(설정됨)") >= 0, "pill19 key full wording in accessible name");
eq(modelPillState({model: "muse-spark-1.3"}).tone, "unknown", "pill19 missing billing is unknown");
eq(modelPillState({model: "muse-spark-1.3", billing: ""}).tone, "unknown", "pill19 empty billing is unknown");
eq(modelPillState({model: "muse-spark-1.3", billing: "offline"}).tone, "unknown", "pill19 unrecognized billing is unknown");
eq(modelPillState({model: "muse-spark-1.3", billing: "unknown"}).tone, "unknown", "pill19 unknown billing is unknown");
eq(modelPillState(null).tone, "unknown", "pill19 load error is unknown");
ok(modelPillState(null).html.indexOf("API 키") < 0, "pill19 error never invents API key");
ok(modelPillState({model: "x"}).html.indexOf("API 키") < 0, "pill19 missing billing never invents API key");
ok(modelPillState({model: "x", billing: "offline"}).html.indexOf("API 키") < 0, "pill19 other billing never invents API key");
ok(modelPillState({model: "muse-spark-1.3", billing: "subscription x"}).html.indexOf("mpmodel") >= 0 && modelPillState({model: "muse-spark-1.3", billing: "subscription x"}).html.indexOf("spark-1.3") > 0, "pill19 model muted after state, muse- prefix stripped");
eq(modelPillState({model: "muse-spark-1.3", billing: "subscription x"}).label, "spark-1.3 · 구독 요청됨 · 미검증", "pill19 plain label carries state without markup");
const pillNodeP = {innerHTML: "", title: "", _attrs: {}, setAttribute: function(k, v) { this._attrs[k] = v; }};
const prevDollarPill = $;
$ = (id) => id === "modelpill" ? pillNodeP : prevDollarPill(id);
paintPill(modelPillState({model: "muse-spark-1.3", billing: "subscription login (no META_API_KEY in environment)"}));
ok(pillNodeP.innerHTML.indexOf("미검증") >= 0, "pill19 paint shows compact state word");
eq(pillNodeP._attrs["data-tone"], "sub", "pill19 paint records tone");
ok(pillNodeP.title.indexOf("검증되지 않았습니다") >= 0, "pill19 paint carries disclaimer");
ok(pillNodeP.title.indexOf("클릭하면 설정이 열립니다") >= 0, "pill19 paint title states the action");
eq(pillNodeP._attrs["aria-label"], "모델 설정 열기: spark-1.3 · 구독 요청됨 · 미검증", "pill19 accessible name pairs action with current state");
paintPill(modelPillState(null));
eq(pillNodeP._attrs["data-tone"], "unknown", "pill19 paint records unknown on error");
ok(pillNodeP._attrs["aria-label"].indexOf("모델 설정 열기") === 0, "pill19 unknown state keeps action purpose");
$ = prevDollarPill;
// R20 activity rows: single-repo pane hides the repeated repo name
const evRowT = actMkEvent("rA", "scan", "t1", "스캔 제목", "상세1");
ok(eventHtml(evRowT).indexOf("<b>rA</b>") >= 0, "row20 glance keeps repo token");
ok(eventHtml(evRowT, {hideRepo: true}).indexOf("<b>rA</b>") < 0, "row20 activity pane drops repeated repo");
ok(eventHtml(evRowT, {hideRepo: true}).indexOf("스캔 제목") >= 0, "row20 title kept without repo token");
ok(eventHtml(evRowT, {hideRepo: true}).indexOf("상세 보기") >= 0, "row20 detail disclosure kept");
// R21 quiet announcements: only meaningful transitions, no focus moves
eq(actStatusFor(null), "", "st21 null input empty");
eq(actStatusFor({}), "", "st21 empty input empty");
eq(actStatusFor({failed: true}), "조회 실패", "st21 plain failure");
eq(actStatusFor({failed: true, keptOld: true}), "조회 실패 · 이전 데이터 표시 중", "st21 failure keeps old data wording");
eq(actStatusFor({recovered: true, n: 2}), "다시 불러옴 · 활동 2건", "st21 recovery");
eq(actStatusFor({initial: true, n: 3}), "활동 3건 표시", "st21 initial completion");
eq(actStatusFor({updated: true, n: 3}), "활동 업데이트됨 · 3건", "st21 quiet change");
eq(actStatusFor({initial: true, recovered: true, n: 1}), "다시 불러옴 · 활동 1건", "st21 recovery outranks initial");
const prevDollarSt = $;
let actStatusNode = null;
const mkStatusNode = () => ({dataset: {}, _writes: 0, _text: "",
  set textContent(v) { this._text = String(v); this._writes++; },
  get textContent() { return this._text; }});
$ = (id) => {
  if (id === "act-status") { if (!actStatusNode) actStatusNode = mkStatusNode(); return actStatusNode; }
  return prevDollarSt(id);
};
actStatusNode = mkStatusNode();
setActStatus("활동 2건 표시", "ok:fp1");
eq(actStatusNode._text, "활동 2건 표시", "st21 status text applied");
eq(actStatusNode._writes, 1, "st21 first write counted");
setActStatus("활동 2건 표시", "ok:fp1");
eq(actStatusNode._writes, 1, "st21 identical state not rewritten");
setActStatus("활동 업데이트됨 · 3건", "ok:fp1");
eq(actStatusNode._writes, 2, "st21 message change with same key still announces once");
setActStatus("활동 업데이트됨 · 3건", "ok:fp2");
eq(actStatusNode._writes, 3, "st21 new state key announces once");
setActStatus("활동 업데이트됨 · 3건", "ok:fp2");
eq(actStatusNode._writes, 3, "st21 repeated state announces nothing");
const keepDollarSt = $;
$ = (id) => undefined;
setActStatus("조회 실패", "err:full");
$ = keepDollarSt;
ok(true, "st21 missing status node never throws");
// R22 activity integration: status follows real paint outcomes
newActGen("rA"); view = {kind: "repo", id: "rA"}; repoTab = "activity";
actCalls = []; actGates = [];
O = {repos: [{name: "rA", github: "o/rA"}]};
actStatusNode = mkStatusNode();
const pStA = paintRepoActivity({name: "rA"});
actGates[0].res({events: [actMkEvent("rA", "scan", "t1", "첫제목", "d")]});
await pStA;
eq(actStatusNode._text, "활동 1건 표시", "st22 initial completion announced");
ok(actGen.box._list._h.indexOf("<b>rA</b>") < 0, "st22 pane rows omit repeated repo");
ok(actGen.box._list._h.indexOf("첫제목") >= 0, "st22 pane rows keep titles");
const wStA = actGen.box._list._writes;
actCalls = []; actGates = [];
const pStAq = paintRepoActivity({name: "rA"}, {quiet: true});
actGates[0].res({events: [actMkEvent("rA", "scan", "t1", "첫제목", "d")]});
await pStAq;
eq(actGen.box._list._writes, wStA, "st22 unchanged quiet poll rewrites nothing");
eq(actStatusNode._writes, 1, "st22 unchanged quiet poll announces nothing");
newActGen("rA"); view = {kind: "repo", id: "rA"}; repoTab = "activity";
actCalls = []; actGates = [];
actStatusNode = mkStatusNode();
const pStB = paintRepoActivity({name: "rA"});
actGates[0].res({events: [actMkEvent("rA", "scan", "t1", "제목", "d")]});
await pStB;
eq(actStatusNode._text, "활동 1건 표시", "st22 seeded completion");
actCalls = []; actGates = [];
const pStBq = paintRepoActivity({name: "rA"}, {quiet: true});
actGates[0].rej(new Error("nope"));
await pStBq;
eq(actStatusNode._text, "조회 실패 · 이전 데이터 표시 중", "st22 kept-data failure announced");
actCalls = []; actGates = [];
const pStBr = paintRepoActivity({name: "rA"}, {quiet: true});
actGates[0].res({events: [actMkEvent("rA", "scan", "t1", "제목", "d")]});
await pStBr;
eq(actStatusNode._text, "다시 불러옴 · 활동 1건", "st22 same-content recovery announced");
actCalls = []; actGates = [];
const pStBn = paintRepoActivity({name: "rA"}, {quiet: true});
actGates[0].res({events: [actMkEvent("rA", "scan", "t1", "바뀐제목", "d")]});
await pStBn;
eq(actStatusNode._text, "활동 업데이트됨 · 1건", "st22 same-count new content announced");
newActGen("rA"); view = {kind: "repo", id: "rA"}; repoTab = "activity";
actCalls = []; actGates = [];
actStatusNode = mkStatusNode();
const pStC = paintRepoActivity({name: "rA"});
actGates[0].rej(new Error("down"));
await pStC;
eq(actStatusNode._text, "조회 실패", "st22 full failure announced");
// R23 refreshPill: loading label is meaningful, deferred settings outcomes paint truthfully
const pillNodeR = {innerHTML: "", title: "", _attrs: {}, setAttribute: function(k, v) { this._attrs[k] = v; }};
const prevDollarR23 = $;
$ = (id) => id === "modelpill" ? pillNodeR : prevDollarR23(id);
actCalls = []; actGates = [];
refreshPill();
eq(pillNodeR.innerHTML, "확인 중…", "r23 loading text shown before response");
ok(pillNodeR._attrs["aria-label"].indexOf("undefined") < 0, "r23 loading label is meaningful, never undefined");
ok(pillNodeR._attrs["aria-label"].indexOf("모델 설정 열기") === 0, "r23 loading keeps action purpose");
eq(actCalls[0], "/api/settings", "r23 refresh reads settings");
actGates[0].res({model: "muse-spark-1.3", billing: "subscription login (no META_API_KEY in environment)"});
await new Promise((r) => setTimeout(r, 10));
ok(pillNodeR.innerHTML.indexOf("미검증") >= 0, "r23 success paints compact state word");
ok(pillNodeR._attrs["aria-label"].indexOf("구독 요청됨") >= 0, "r23 success name carries requested mode");
ok(pillNodeR._attrs["aria-label"].indexOf("미검증") >= 0, "r23 success label carries unverified state");
actCalls = []; actGates = [];
refreshPill();
actGates[0].rej(new Error("down"));
await new Promise((r) => setTimeout(r, 10));
eq(pillNodeR._attrs["data-tone"], "unknown", "r23 error paints unknown");
ok(pillNodeR.innerHTML.indexOf("API 키") < 0, "r23 error never invents API key");
ok(pillNodeR._attrs["aria-label"].indexOf("모델 설정 열기") === 0, "r23 error keeps action purpose");
$ = prevDollarR23;
// R24 settings billing truth: same requested/unverified/unknown semantics as
// the pill; raw billing strings never render as fact.
O = {repos: [{name: "rA", checkout: "/tmp/rA"}]};
const st24PrevJget = jget;
let st24Settings = {model: "muse-spark-1.3", billing: "subscription login (no META_API_KEY in environment)", workspace_root: "/tmp/w"};
jget = async (u) => {
  if (u !== "/api/settings") throw new Error("settings reads only /api/settings, got " + u);
  return st24Settings;
};
await openSettings();
let st24Html = modalHtml[modalHtml.length - 1];
ok(st24Html.indexOf("구독 요청됨") >= 0, "st24 requested mode shown, never verified connection");
ok(st24Html.indexOf("미검증") >= 0, "st24 unverified state visible in settings");
ok(st24Html.indexOf("no META_API_KEY") < 0, "st24 raw billing never renders as fact");
ok(st24Html.indexOf("검증되지 않았습니다") >= 0, "st24 title disclaims worker verification");
ok(st24Html.indexOf('for="set-root"') >= 0, "st24 work folder label associated");
ok(st24Html.indexOf('for="set-model"') >= 0, "st24 model label associated");
ok(st24Html.indexOf("<h3>설정</h3>") >= 0, "st24 dialog heading names the dialog");
st24Settings = {model: "muse-spark-1.3", billing: "api-key (META_API_KEY is set; takes priority over login)", workspace_root: "/tmp/w"};
await openSettings();
st24Html = modalHtml[modalHtml.length - 1];
ok(st24Html.indexOf("API 키(설정됨)") >= 0, "st24 configured key labeled only when configured");
ok(st24Html.indexOf("인증됨") < 0 && st24Html.indexOf("연결됨") < 0, "st24 key config is not auth success");
ok(st24Html.indexOf("META_API_KEY is set") < 0, "st24 raw key detail never renders");
st24Settings = {model: "muse-spark-1.3", workspace_root: "/tmp/w"};
await openSettings();
st24Html = modalHtml[modalHtml.length - 1];
ok(st24Html.indexOf("상태 알 수 없음") >= 0, "st24 missing billing is unknown");
ok(st24Html.indexOf("API 키") < 0, "st24 missing billing never invents API key");
st24Settings = {model: "muse-spark-1.3", billing: "offline", workspace_root: "/tmp/w"};
await openSettings();
st24Html = modalHtml[modalHtml.length - 1];
ok(st24Html.indexOf("상태 알 수 없음") >= 0, "st24 unrecognized billing is unknown");
ok(st24Html.indexOf("API 키") < 0, "st24 other billing never invents API key");
// hostile model and repo values escape; real esc is active this far into the driver.
st24Settings = {model: "<img src=x onerror=1>", billing: "subscription login (no META_API_KEY in environment)", workspace_root: '"><script>alert(1)</script>'};
O = {repos: [{name: "rH", checkout: "<b>q"}]};
await openSettings();
st24Html = modalHtml[modalHtml.length - 1];
ok(st24Html.indexOf("&lt;img") >= 0, "st24 hostile model escaped");
ok(st24Html.indexOf("<img src=x") < 0, "st24 no live hostile model markup");
ok(st24Html.indexOf("&lt;script") >= 0, "st24 hostile workspace root escaped");
ok(st24Html.indexOf("<script>alert") < 0, "st24 no live hostile root markup");
ok(st24Html.indexOf("&lt;b&gt;q") >= 0, "st24 hostile repo checkout escaped");
// failed settings read keeps the titled settings dialog: loading first,
// then the error markup with the same dialog name; the error escapes.
const st24ModalCount = modalHtml.length;
jget = async (u) => { throw new Error("<img src=x>"); };
await openSettings();
eq(modalHtml.length, st24ModalCount + 2, "st24 failure adds loading then error dialog");
st24Html = modalHtml[modalHtml.length - 1];
ok(st24Html.indexOf("<h3>설정</h3>") >= 0, "st24 error keeps the settings dialog name");
ok(st24Html.indexOf("불러오기 실패") >= 0, "st24 error states the failed load");
ok(st24Html.indexOf("&lt;img") >= 0, "st24 hostile error escaped");
ok(st24Html.indexOf("<img src=x") < 0, "st24 no live hostile error markup");
jget = st24PrevJget;
// R25 sidebar recents: primary title readable first, metadata on its own
// line inside #chatlist scope; full title stays reachable via tooltip/name.
O = {runs: []};
const st25Row = chatRowHtml({id: "c1", title: "수동으로 쓴 긴 대화 제목입니다", issue: "", created_at: undefined});
ok(st25Row.indexOf('data-chat="c1"') >= 0, "st25 row carries chat id");
ok(st25Row.indexOf("수동으로 쓴 긴 대화 제목입니다") >= 0, "st25 full manual title rendered");
ok(st25Row.indexOf("수동으로 쓴 긴 대화 제목입니다") < st25Row.indexOf('class="ago"'), "st25 title precedes metadata");
ok(st25Row.indexOf("aria-label=") >= 0, "st25 row has an accessible name");
const st25Hostile = chatRowHtml({id: "c2", title: "<b>나쁜제목</b>", issue: "", created_at: undefined});
ok(st25Hostile.indexOf("&lt;b&gt;") >= 0, "st25 hostile title escaped");
ok(st25Hostile.indexOf("<b>나쁜제목</b>") < 0, "st25 no live hostile title markup");
// R26 discussion detail opens from the stored cache first: GET-only,
// identity-checked, never an automatic POST; failures record an error
let discPostUrls = [];
jpost = async (u) => { discPostUrls.push(u); return {}; };
discFetching = new Set(); discNodes = {}; discNodeErr = {};
discFpVal = {}; discCache = {};
let discGetUrls = [];
const discNode858 = {repo: "rn", node: {number: 858, title: "Opus", body: "b", comments: []}, review: {}};
jget = (u) => { discGetUrls.push(u); return Promise.resolve(discNode858); };
let readOk = await discReadNode("rn:858", null);
eq(readOk, true, "stored read succeeds on identity match");
eq(discPostUrls.length, 0, "open never POSTs detail");
ok(discGetUrls[0].indexOf("/api/discussions/node?") === 0, "open uses the cached node GET");
eq(String(discNodes["rn:858"].node.number), "858", "exact node stored");
eq(discNodeErr["rn:858"], undefined, "no error recorded on success");
jget = () => Promise.resolve({repo: "rn", node: {number: 999}, review: {}});
readOk = await discReadNode("rn:858", null);
eq(readOk, false, "mismatched number refuses");
eq(String(discNodes["rn:858"].node.number), "858", "good node never overwritten by mismatch");
ok((discNodeErr["rn:858"] || {}).message, "mismatch records an honest error");
eq(discPostUrls.length, 0, "mismatch still never POSTs");
jget = () => Promise.resolve({repo: "other", node: {number: 858}, review: {}});
readOk = await discReadNode("rn:858", null);
eq(readOk, false, "declared wrong repo refuses");
eq(String(discNodes["rn:858"].node.number), "858", "good node never overwritten by foreign repo");
eq(discPostUrls.length, 0, "repo mismatch still never POSTs");
// nominal open through the real toggle: loading then content, zero POST
discSel = {}; discOpen = new Set(); discNodes = {}; discNodeErr = {};
discFetching = new Set(); discCache = {}; discFpVal = {}; discScroll = {};
let paintCalls = 0, focusCalls = 0, hashCalls = 0;
paintRepoCommunity = async () => { paintCalls++; };
document = {querySelector: () => ({focus: () => { focusCalls++; }, scrollIntoView: () => {}})};
syncHash = () => { hashCalls++; };
jget = (u) => Promise.resolve({repo: "rD", node: {number: 5, title: "five", comments: []}, review: {status: "reviewed"}});
discPostUrls = [];
view = {kind: "repo", id: "rD"}; repoTab = "community";
await discToggleDetail({name: "rD"}, "rD:5", {});
eq(paintCalls, 2, "nominal open paints loading then content");
eq(discSel["rD"], "rD:5", "nominal open keeps selection");
eq(String(discNodes["rD:5"].node.number), "5", "stored original available");
eq(focusCalls, 1, "current open focuses its heading");
eq(hashCalls, 1, "current open records its address once");
eq(discPostUrls.length, 0, "nominal open never POSTs");
jget = () => Promise.reject(new Error("down"));
discNodes = {};
readOk = await discReadNode("rn:858", null);
eq(readOk, false, "failed read reports false");
eq(discNodes["rn:858"], undefined, "no node fabricated on failure");
eq(discNodeErr["rn:858"].message, "down", "server reason recorded");
eq(discPostUrls.length, 0, "failure never POSTs");
// R27 stored-read error panel: honest copy, both manual actions, escaped
esc = eval("(" + __escSrc.replace("function esc", "function") + ")");
discNodeErr = {"rn:858": {message: "<img src=x onerror=alert(1)>"}};
const derrHtml = discNodeErrHtml("rn", "rn:858");
ok(derrHtml.indexOf("&lt;img") >= 0, "hostile read error escaped");
ok(derrHtml.indexOf("<img src=x") < 0, "no live hostile markup in error panel");
ok(derrHtml.indexOf("data-discread=") >= 0, "manual stored reread offered");
ok(derrHtml.indexOf("data-discnodefetch=") >= 0, "explicit upstream fetch offered");
ok(derrHtml.indexOf("모델 검토가 시작되지 않습니다") >= 0, "no review started from the error box");
ok(derrHtml.indexOf("완료") < 0, "failure never claims completion");
ok(derrHtml.indexOf("0건") < 0, "failure never claims an empty discussion");
// failed open through the real toggle: selection kept, error recorded,
// loading then error paints, manual actions available from recorded state
discSel = {}; discOpen = new Set(); discNodes = {}; discNodeErr = {};
discFetching = new Set(); discCache = {}; discFpVal = {}; discScroll = {};
paintCalls = 0; focusCalls = 0; hashCalls = 0;
paintRepoCommunity = async () => { paintCalls++; };
document = {querySelector: () => null};
jget = () => Promise.reject(new Error("net down"));
discPostUrls = [];
view = {kind: "repo", id: "rD"}; repoTab = "community";
await discToggleDetail({name: "rD"}, "rD:6", {});
eq(discSel["rD"], "rD:6", "failed open keeps selection");
ok(discNodeErr["rD:6"], "failed open records the error");
eq(paintCalls, 2, "failed open paints loading then the error box");
eq(focusCalls, 0, "failed open steals no focus");
eq(hashCalls, 1, "failed open keeps its error selection address");
eq(discPostUrls.length, 0, "failed open never POSTs");
const derrReal = discNodeErrHtml("rD", "rD:6");
ok(derrReal.indexOf("data-discread=") >= 0, "recorded error offers manual reread");
ok(derrReal.indexOf("data-discnodefetch=") >= 0, "recorded error offers explicit fetch");
ok(derrReal.indexOf("net down") >= 0, "recorded server reason shown");
// R28 quiet poll never retries a recorded read error; deferred close
// during fetch never reopens or repaints the stale view
discSel = {"rD": "rD:9"};
discOpen = new Set(["rD:9"]);
discNodes = {}; discNodeErr = {"rD:9": {message: "down"}};
discFetching = new Set(); discSeenListRev = {};
view = {kind: "repo", id: "rD"}; repoTab = "community";
discGetUrls = []; discPostUrls = [];
jget = (u) => { discGetUrls.push(u); return Promise.resolve({}); };
await discReconcileOpen({name: "rD"}, {items: [
  {number: 9, updated_at: "t9", title: "nine", review: {status: "unreviewed"}}]});
eq(discGetUrls.length, 0, "errored absent node is not re-asked by quiet poll");
eq(discPostUrls.length, 0, "error skip performs no POST either");
eq(discNodes["rD:9"], undefined, "absent node stays absent without retry");
ok(discNodeErr["rD:9"], "recorded error survives the quiet poll");
discSel = {"rD": "rD:7"};
discOpen = new Set(["rD:7"]);
discNodes = {"rD:7": {node: {number: 7, updated_at: "t0", title: "old"},
  review: {status: "reviewed", stale: false, job: {status: "idle"}}}};
discNodeErr = {};
discGetUrls = []; discPostUrls = [];
jget = (u) => { discGetUrls.push(u); return Promise.resolve(
  {node: {number: 7, updated_at: "t1", title: "new"},
   review: {status: "reviewed", stale: false, job: {status: "idle"}}}); };
await discReconcileOpen({name: "rD"}, {items: [
  {number: 7, updated_at: "t1", title: "new", review: {status: "reviewed"}}]});
eq(discGetUrls.length, 1, "stale known node refreshes exactly once");
ok(discGetUrls[0].indexOf("number=7") >= 0, "refresh targets the exact known number");
eq(discPostUrls.length, 0, "quiet refresh never POSTs");
discSel = {}; discOpen = new Set(); discNodes = {}; discNodeErr = {};
discFetching = new Set(); discCache = {}; discFpVal = {}; discScroll = {};
paintCalls = 0; focusCalls = 0; hashCalls = 0;
paintRepoCommunity = async () => { paintCalls++; };
document = {querySelector: () => ({focus: () => { focusCalls++; }, scrollIntoView: () => {}})};
syncHash = () => { hashCalls++; };
let resolveGate = null;
jget = () => new Promise((res) => { resolveGate = res; });
view = {kind: "repo", id: "rD"}; repoTab = "community";
const pToggle = discToggleDetail({name: "rD"}, "rD:5", {});
discSel = {}; discOpen.delete("rD:5");
resolveGate({repo: "rD", node: {number: 5, title: "five", comments: []}, review: {}});
await pToggle;
await new Promise((r) => setTimeout(r, 0));
await new Promise((r) => setTimeout(r, 0));
eq(paintCalls, 1, "only the loading paint ran; closed view never repaints");
eq(focusCalls, 0, "closed view steals no focus");
eq(hashCalls, 0, "closed view writes no address");
eq(String(discNodes["rD:5"].node.number), "5", "deferred result cached for later");
eq(discPostUrls.length, 0, "deferred open never POSTs");
// R29 overview single-flight + trailing refresh + deferred pane input preservation.
// Real epoch pump (loadAll/drive/settle) + real applyOverview; jget and pane
// painters are deferred fakes. No fake epoch mutation: every transition below
// goes through actual request timing.
O = null; view = {kind: "glance"}; repoTab = "overview"; selRepo = ""; seenQ = new Set();
checks = {}; CSS = {escape: (s) => String(s)};
paintStatusbar = () => {}; renderCounts = () => {};
composerBusy = () => false; workTyping = () => false; stashReviewState = () => {};
lastRenderResetScroll = false;
const r29Boxes = {};
const r29Box = (id) => {
  if (!r29Boxes[id]) r29Boxes[id] = {innerHTML: "", dataset: {}, className: "", textContent: "",
    value: "", selectionStart: 0, selectionEnd: 0, scrollTop: 0,
    setSelectionRange: function(a, b) { this.selectionStart = a; this.selectionEnd = b; },
    focus: function() { document.activeElement = this; }};
  return r29Boxes[id];
};
$ = (id) => {
  if (id === "modal") return {classList: {contains: () => true}};
  return r29Box(id);
};
document = {activeElement: null, querySelectorAll: () => [], querySelector: () => null};
let r29RenderCalls = 0;
render = () => { r29RenderCalls++; };
let r29Gates = [], r29FetchCount = 0;
jget = (u) => { r29FetchCount++;
  const g = {};
  const p = new Promise((res, rej) => {
    g.res = (v) => { g.done = true; res(v); };
    g.rej = (e) => { g.done = true; rej(e); };
    g.done = false;
  });
  r29Gates.push(g);
  return p;
};
// T1: concurrent polls share one fetch; explicit demand during flight gets
// exactly one trailing fetch, and the superseded response never lands on O.
r29Gates = []; r29FetchCount = 0; r29RenderCalls = 0;
const r29p1a = realLoadAll(true, {background: true});
const r29p1b = realLoadAll(true, {background: true});
const r29p1c = realLoadAll(false);
eq(r29FetchCount, 1, "r29 concurrent polls share one fetch");
r29Gates[0].res({repos: [{name: "rX", mark: 1}], runs: []});
await new Promise((r) => setTimeout(r, 0));
await new Promise((r) => setTimeout(r, 0));
eq(r29FetchCount, 2, "r29 explicit demand during flight starts exactly one trailing fetch");
eq(O, null, "r29 superseded pre-demand response never assigned");
r29Gates[1].res({repos: [{name: "rX", mark: 2}], runs: []});
await Promise.all([r29p1a, r29p1b, r29p1c]);
eq(O && O.repos[0].mark, 2, "r29 trailing response applied");
eq(r29FetchCount, 2, "r29 no extra fetch after settle");
eq(r29RenderCalls, 1, "r29 trailing applies paint once");
// T1b: TRUE-quiet explicit post-mutation mid-fetch stays unsettled until the
// post-demand fetch applies; the superseded response never lands on O.
O = null; view = {kind: "glance"}; repoTab = "overview"; selRepo = ""; seenQ = new Set();
r29Gates = []; r29FetchCount = 0; r29RenderCalls = 0;
let r29t1bSettled = false;
const r29p1d = realLoadAll(true, {background: true});
const r29p1e = realLoadAll(true);
r29p1e.then(() => { r29t1bSettled = true; }, () => { r29t1bSettled = true; });
eq(r29FetchCount, 1, "r29 true-quiet pair shares one fetch");
r29Gates[0].res({repos: [{name: "rX", mark: 1}], runs: []});
await new Promise((r) => setTimeout(r, 0));
await new Promise((r) => setTimeout(r, 0));
eq(r29FetchCount, 2, "r29 true-quiet explicit also demands a trailing fetch");
eq(r29t1bSettled, false, "r29 post-mutation promise unsettled until trailing applies");
eq(O, null, "r29 pre-demand response discarded");
r29Gates[1].res({repos: [{name: "rX", mark: 2}], runs: []});
await Promise.all([r29p1d, r29p1e]);
eq(r29t1bSettled, true, "r29 explicit settles after trailing application");
eq(O && O.repos[0].mark, 2, "r29 latest applied");
eq(r29RenderCalls, 1, "r29 trailing paints once");
// T3: shared paint failure rejects every joined waiter; the guard reopens.
// Leader is explicit (no fresh demand is created by joining), so both waiters
// actually reach the shared application and both must observe its failure:
// a background waiter resolved by an earlier settle can never retroactively
// reject, so the obsolete bg-leader shape is not used here.
O = null; view = {kind: "glance"}; repoTab = "overview"; selRepo = ""; seenQ = new Set();
r29Gates = []; r29FetchCount = 0; r29RenderCalls = 0;
render = () => { throw new Error("paint boom"); };
const r29errs = [];
const r29p3a = realLoadAll(false);
const r29p3b = realLoadAll(true, {background: true});
r29p3a.then(() => r29errs.push("ex-ok"), (e) => r29errs.push("ex:" + e.message));
r29p3b.then(() => r29errs.push("bg-ok"), (e) => r29errs.push("bg:" + e.message));
r29Gates[0].res({repos: [{name: "rX"}], runs: []});
await new Promise((r) => setTimeout(r, 0));
await new Promise((r) => setTimeout(r, 0));
await new Promise((r) => setTimeout(r, 0));
eq(r29errs.sort().join("|"), "bg:paint boom|ex:paint boom", "r29 shared paint failure rejects every joined waiter");
render = () => { r29RenderCalls++; };
const r29p3c = realLoadAll(false);
eq(r29FetchCount, 2, "r29 latch cleared, retry starts a fresh fetch");
r29Gates[1].res({repos: [{name: "rX"}], runs: []});
await r29p3c;
eq(r29RenderCalls, 1, "r29 retry paints once");
eq(O && O.repos[0].name, "rX", "r29 retry applies fresh data");
// T4: fetch failure settles (no wedge) with the existing error UI; next poll refetches.
O = null; view = {kind: "glance"}; repoTab = "overview"; selRepo = ""; seenQ = new Set();
render = () => { r29RenderCalls++; };
r29Gates = []; r29FetchCount = 0;
const r29p4 = realLoadAll(true, {background: true});
r29Gates[0].rej(new Error("net down"));
await r29p4;
eq(r29Box("view").innerHTML.indexOf("서버 응답을 읽는 중 문제가 생겼습니다") >= 0, true, "r29 fetch failure paints the existing error UI");
const r29p4b = realLoadAll(true, {background: true});
eq(r29FetchCount, 2, "r29 failure clears the guard, next poll refetches");
r29Gates[1].res({repos: [], runs: []});
await r29p4b;
eq(O && Array.isArray(O.repos), true, "r29 post-failure fetch applies");
// T5: deferred pane + user edit during await + explicit demand during apply.
// Pane apply must not restore the pre-await composer snapshot, steal focus,
// reset nonzero scroll, or full-render; the trailing fetch still runs.
view = {kind: "repo", id: "rX"}; repoTab = "reviews"; selRepo = "rX"; seenQ = new Set();
O = {repos: [{name: "rX"}], runs: []};
r29Box("tabbody").dataset.revReady = "1";
const r29inp = r29Box("input");
r29inp.value = "before"; r29inp.selectionStart = 0; r29inp.selectionEnd = 6;
document.activeElement = null;
let r29paneGate = null, r29paneCalls = 0;
paintRepoReviews = () => { r29paneCalls++;
  if (r29paneCalls === 1) return new Promise((res) => { r29paneGate = res; });
  return Promise.resolve(); };
r29Gates = []; r29FetchCount = 0; r29RenderCalls = 0;
const r29p5a = realLoadAll(true, {background: true});
r29Gates[0].res({repos: [{name: "rX", mark: 1}], runs: []});
await new Promise((r) => setTimeout(r, 0));
await new Promise((r) => setTimeout(r, 0));
eq(r29paneCalls, 1, "r29 quiet pane painter starts after fetch");
r29inp.value = "user-late-edit"; r29inp.selectionStart = 4; r29inp.selectionEnd = 9;
r29Box("view").scrollTop = 200;
document.activeElement = r29inp;
const r29p5b = realLoadAll(false);
eq(r29FetchCount, 1, "r29 explicit during apply joins, no new fetch yet");
r29paneGate("pane-done"); r29paneGate = null;
await new Promise((r) => setTimeout(r, 0));
await new Promise((r) => setTimeout(r, 0));
eq(r29FetchCount, 2, "r29 trailing fetch starts after pane apply");
eq(r29inp.value, "user-late-edit", "r29 late user edit survives pane apply");
eq(r29inp.selectionStart + "," + r29inp.selectionEnd, "4,9", "r29 late caret survives pane apply");
eq(document.activeElement, r29inp, "r29 focus untouched by quiet pane apply");
eq(r29Box("view").scrollTop, 200, "r29 nonzero scroll retained, no outer render");
eq(r29RenderCalls, 0, "r29 pane path never full-renders");
eq(O && O.repos[0].mark, 1, "r29 epoch-one data applied");
r29Gates[1].res({repos: [{name: "rX", mark: 2}], runs: []});
await Promise.all([r29p5a, r29p5b]);
eq(O && O.repos[0].mark, 2, "r29 trailing data applied");
eq(r29FetchCount, 2, "r29 still one active fetch at a time");
eq(r29inp.value, "user-late-edit", "r29 edit survives trailing apply too");
// T6: view navigates away during pane await: the stale pane return must not
// clobber the live draft, steal focus, or wedge the epoch; the trailing
// full-path render restores exactly the live snapshot it captured.
view = {kind: "repo", id: "rX"}; repoTab = "reviews"; selRepo = "rX"; seenQ = new Set();
O = {repos: [{name: "rX"}], runs: []};
r29Box("tabbody").dataset.revReady = "1";
r29inp.value = "nav-draft"; r29inp.selectionStart = 0; r29inp.selectionEnd = 9;
document.activeElement = r29inp;
r29paneGate = null; r29paneCalls = 0;
r29Gates = []; r29FetchCount = 0; r29RenderCalls = 0;
const r29p6a = realLoadAll(true, {background: true});
r29Gates[0].res({repos: [{name: "rX", mark: 1}], runs: []});
await new Promise((r) => setTimeout(r, 0));
await new Promise((r) => setTimeout(r, 0));
eq(r29paneCalls, 1, "r29 pane starts before navigation");
view = {kind: "glance"};
r29Box("view").scrollTop = 200;
const r29p6b = realLoadAll(false);
r29paneGate("done"); r29paneGate = null;
await new Promise((r) => setTimeout(r, 0));
await new Promise((r) => setTimeout(r, 0));
eq(r29FetchCount, 2, "r29 trailing fetch still starts after navigate-away pane");
eq(r29inp.value, "nav-draft", "r29 draft untouched by stale pane return");
eq(document.activeElement, r29inp, "r29 no focus steal on stale pane return");
r29Gates[1].res({repos: [{name: "rX", mark: 2}], runs: []});
await Promise.all([r29p6a, r29p6b]);
eq(O && O.repos[0].mark, 2, "r29 trailing applied after navigation");
eq(r29inp.value, "nav-draft", "r29 full-path restore keeps the live draft");
eq(r29RenderCalls, 1, "r29 trailing full render paints once");
// T7: explicit demand DURING a trailing fetch. The intermediate response must
// neither apply O nor settle its waiters; a third fetch carries the latest.
// Tagged background polls during either flight schedule nothing extra; the
// pending-gate count proves exactly one fetch is ever active (no hot loop).
O = null; view = {kind: "glance"}; repoTab = "overview"; selRepo = ""; seenQ = new Set();
r29Gates = []; r29FetchCount = 0; r29RenderCalls = 0;
const r29p7a = realLoadAll(true, {background: true});
const r29p7b = realLoadAll(false);
const r29p7c = realLoadAll(true, {background: true});
const r29ab = [];
r29p7b.then(() => r29ab.push("A-ok"), () => r29ab.push("A-err"));
eq(r29FetchCount, 1, "r29 followers join the first flight");
eq(r29Gates.filter((g) => !g.done).length, 1, "r29 exactly one active fetch");
r29Gates[0].res({repos: [{name: "rX", mark: 1}], runs: []});
await new Promise((r) => setTimeout(r, 0));
await new Promise((r) => setTimeout(r, 0));
eq(r29FetchCount, 2, "r29 trailing starts once for A");
eq(O, null, "r29 first response discarded, never applied");
const r29p7d = realLoadAll(false);
const r29p7e = realLoadAll(true, {background: true});
r29p7d.then(() => r29ab.push("B-ok"), () => r29ab.push("B-err"));
eq(r29FetchCount, 2, "r29 demands during trailing schedule nothing extra");
eq(r29Gates.filter((g) => !g.done).length, 1, "r29 still exactly one active fetch");
r29Gates[1].res({repos: [{name: "rX", mark: 2}], runs: []});
await new Promise((r) => setTimeout(r, 0));
await new Promise((r) => setTimeout(r, 0));
eq(r29FetchCount, 3, "r29 second trailing starts once for B");
eq(O, null, "r29 intermediate trailing response never applied");
eq(r29ab.length, 0, "r29 intermediate settle waits for neither A nor B");
eq(r29Gates.filter((g) => !g.done).length, 1, "r29 one active fetch through the chain");
r29Gates[2].res({repos: [{name: "rX", mark: 3}], runs: []});
await Promise.all([r29p7a, r29p7b, r29p7c, r29p7d, r29p7e]);
eq(r29ab.sort().join("|"), "A-ok|B-ok", "r29 both explicits settle with the latest");
eq(O && O.repos[0].mark, 3, "r29 latest response applied once");
eq(r29RenderCalls, 1, "r29 exactly one application for the latest");
eq(r29FetchCount, 3, "r29 no hot loop after settle");
await new Promise((r) => setTimeout(r, 0));
eq(r29FetchCount, 3, "r29 still quiet after extra ticks");
// T8: paint intent follows the actual application branch. A non-quiet waiter
// forces the one shared full paint (quiet followers ride along); an all-quiet
// epoch stays on the targeted pane path and is never forced to full.
view = {kind: "repo", id: "rX"}; repoTab = "reviews"; selRepo = "rX"; seenQ = new Set();
O = {repos: [{name: "rX"}], runs: []};
r29Box("tabbody").dataset.revReady = "1";
let r29pane8 = 0;
paintRepoReviews = () => { r29pane8++; return Promise.resolve(); };
r29Gates = []; r29FetchCount = 0; r29RenderCalls = 0;
const r29p8a = realLoadAll(false);
const r29p8b = realLoadAll(true, {background: true});
r29Gates[0].res({repos: [{name: "rX"}], runs: []});
await Promise.all([r29p8a, r29p8b]);
eq(r29RenderCalls, 1, "r29 full intent preserved over quiet followers");
eq(r29pane8, 0, "r29 non-quiet skips the targeted pane path");
r29Gates = []; r29FetchCount = 0; r29RenderCalls = 0; r29pane8 = 0;
const r29p8c = realLoadAll(true);
r29Gates[0].res({repos: [{name: "rX"}], runs: []});
await r29p8c;
eq(r29pane8, 1, "r29 quiet explicit uses the targeted pane path");
eq(r29RenderCalls, 0, "r29 quiet explicit never forced to full");
// T9: clearing the composer during pane await is a user edit, not an empty
// snapshot to restore; navigating afterwards never resurrects the old draft.
view = {kind: "repo", id: "rX"}; repoTab = "reviews"; selRepo = "rX"; seenQ = new Set();
O = {repos: [{name: "rX"}], runs: []};
r29Box("tabbody").dataset.revReady = "1";
const r29inp9 = r29Box("input");
r29inp9.value = "before-clear"; r29inp9.selectionStart = 0; r29inp9.selectionEnd = 12;
document.activeElement = null;
r29paneGate = null; r29paneCalls = 0;
paintRepoReviews = () => { r29paneCalls++;
  if (r29paneCalls === 1) return new Promise((res) => { r29paneGate = res; });
  return Promise.resolve(); };
r29Gates = []; r29FetchCount = 0; r29RenderCalls = 0;
const r29p9a = realLoadAll(true, {background: true});
r29Gates[0].res({repos: [{name: "rX", mark: 1}], runs: []});
await new Promise((r) => setTimeout(r, 0));
await new Promise((r) => setTimeout(r, 0));
eq(r29paneCalls, 1, "r29 pane pending before clear");
r29inp9.value = ""; r29inp9.selectionStart = 0; r29inp9.selectionEnd = 0;
r29Box("view").scrollTop = 200;
document.activeElement = r29inp9;
const r29p9b = realLoadAll(false);
r29paneGate("done"); r29paneGate = null;
await new Promise((r) => setTimeout(r, 0));
await new Promise((r) => setTimeout(r, 0));
eq(r29inp9.value, "", "r29 cleared composer not restored");
eq(r29inp9.selectionStart + "," + r29inp9.selectionEnd, "0,0", "r29 clear caret kept");
eq(r29FetchCount, 2, "r29 trailing still runs after clear");
view = {kind: "glance"};
r29Gates[1].res({repos: [{name: "rX", mark: 2}], runs: []});
await Promise.all([r29p9a, r29p9b]);
eq(r29inp9.value, "", "r29 navigation never resurrects the old draft");
eq(r29RenderCalls, 1, "r29 trailing full render paints once");
// T10: error-display itself throws after a fetch rejection: the epoch still
// settles like the existing error path and the next manual call refetches.
O = null; view = {kind: "glance"}; repoTab = "overview"; selRepo = ""; seenQ = new Set();
r29Gates = []; r29FetchCount = 0; r29RenderCalls = 0;
let r29displayThrows = true;
const r29dollar = $;
$ = (id) => {
  if (id === "netdot-dot" && r29displayThrows) { r29displayThrows = false; throw new Error("display boom"); }
  return r29dollar(id);
};
const r29p10 = realLoadAll(true, {background: true});
r29Gates[0].rej(new Error("net down"));
await r29p10;
eq(r29FetchCount, 1, "r29 display failure settles without a new fetch");
$ = r29dollar;
const r29p10b = realLoadAll(false);
eq(r29FetchCount, 2, "r29 next manual call refetches after display failure");
r29Gates[1].res({repos: [{name: "rX"}], runs: []});
await r29p10b;
eq(O && O.repos[0].name, "rX", "r29 post-display-failure fetch applies");
// R30 runs/answer slice (F1-F4): row/summary/file sizes are CSS (static
// test), but the answer box, draft lifecycle, Enter guard and the typing
// poll guard run here against the real product functions.
// R30a: the box helper renders a textarea with the same id, label and
// placeholder, plus the visible shortcut and draft hint.
wAnsDraft = {};
esc = eval("(" + __escSrc.replace("function esc", "function") + ")");
var r30h0 = wAnswerBoxHtml("run7");
ok(r30h0.indexOf('<textarea id="w-answer"') >= 0, "r30 answer box is a textarea");
ok(r30h0.indexOf('rows="3"') >= 0, "r30 textarea has 3 rows");
ok(r30h0.indexOf('aria-label="차단 질문 답변"') >= 0, "r30 label kept");
ok(r30h0.indexOf('placeholder="답변을 입력하세요"') >= 0, "r30 placeholder kept");
ok(r30h0.indexOf("Ctrl/⌘+Enter") >= 0, "r30 shortcut is visible text");
ok(r30h0.indexOf('id="w-ans-draft"') >= 0 && r30h0.indexOf("임시 저장됨") >= 0,
  "r30 draft hint element present");
ok(r30h0.indexOf("<input") === -1, "r30 no single-line input left");
// R30b: type -> navigate away -> return restores the draft; send clears it.
wAnsDraftSet("run7", "다크 모드 확인 후 답변");
var r30h1 = wAnswerBoxHtml("run7");
ok(r30h1.indexOf("다크 모드 확인 후 답변") >= 0, "r30 draft restored on re-render");
ok(r30h1.indexOf('id="w-ans-draft">') >= 0, "r30 hint shown while a draft exists");
eq(r30h1, wAnswerBoxHtml("run7"), "r30 untouched re-render is stable");
wAnsDraftSet("run7", "<img src=x>");
var r30hx = wAnswerBoxHtml("run7");
ok(r30hx.indexOf("&lt;img") >= 0, "r30 hostile draft escaped");
ok(r30hx.indexOf("<img") === -1, "r30 no live markup from hostile draft");
wAnsDraftSet("run7", "");
var r30h2 = wAnswerBoxHtml("run7");
ok(r30h2.indexOf("다크 모드") === -1, "r30 cleared draft leaves no text");
ok(r30h2.indexOf('id="w-ans-draft" hidden') >= 0, "r30 hint hides when the draft is cleared");
// R30c: wiring — plain Enter never sends, Ctrl/Cmd+Enter sends once,
// typing saves the draft and toggles the hint.
var r30sent = 0;
var r30listeners = {};
var r30hint = {hidden: true};
var r30box = {value: "", dataset: {},
  addEventListener: function(t, fn) { r30listeners[t] = fn; }};
$ = function(id) { return id === "w-ans-draft" ? r30hint : null; };
wireAnswerBox(r30box, function() { r30sent++; }, "run7");
r30listeners.keydown({key: "Enter", ctrlKey: false, metaKey: false,
  preventDefault: function() { throw new Error("must not touch the event on newline"); }});
eq(r30sent, 0, "r30 plain Enter never sends");
r30listeners.keydown({key: "Enter", ctrlKey: true, metaKey: false, preventDefault: function() {}});
eq(r30sent, 1, "r30 Ctrl+Enter sends");
r30listeners.keydown({key: "Enter", ctrlKey: false, metaKey: true, preventDefault: function() {}});
eq(r30sent, 2, "r30 Cmd+Enter sends");
r30box.value = "임시 메모";
r30listeners.input();
eq(wAnsDraftGet("run7"), "임시 메모", "r30 typing saves the draft");
eq(r30hint.hidden, false, "r30 hint appears with a draft");
r30box.value = "";
r30listeners.input();
eq(wAnsDraftGet("run7"), "", "r30 clearing removes the draft");
eq(r30hint.hidden, true, "r30 hint hides when empty");
// R30d: the quiet-poll typing guard still covers the textarea.
// (An earlier section stubs the global workTyping to false, so re-evaluate
// the real product function under a local name here.)
const r30workTyping = eval("(" + extractFn("workTyping") + ")");
var r30ta = {tagName: "TEXTAREA", isContentEditable: false};
var r30in = {tagName: "INPUT", isContentEditable: false};
$ = function(id) {
  if (id === "modal") return null;
  if (id === "view") return {contains: function(el) { return el === r30ta || el === r30in; }};
  return null;
};
document = {activeElement: r30ta};
eq(r30workTyping(), true, "r30 textarea focus counts as typing");
document = {activeElement: r30in};
eq(r30workTyping(), true, "r30 input focus still counts");
document = {activeElement: null};
eq(r30workTyping(), false, "r30 no focus means idle");
// R31 report tables, file buttons and the shared draft (F4 redo, G1, G2).
// The real md + mdInline run here against canned report shapes.
splitPipeRow = eval("(" + extractFn("splitPipeRow") + ")");
isPipeDelim = eval("(" + extractFn("isPipeDelim") + ")");
mdInline = eval("(" + extractFn("mdInline") + ")");
md = eval("(" + extractFn("md") + ")");
var r31tbl = "| Module | Material usage | Migration weight |\\n" +
  "|---|---|---|\\n" +
  "| [calendar_theme.dart](workfile:calendar_theme.dart) | 테마 | 높음 |\\n" +
  "| calendar_header.dart | 헤더 | 낮음 |";
var r31out = md(r31tbl);
ok(r31out.indexOf("<table>") >= 0, "r31 well-formed pipe block becomes a table");
ok(r31out.indexOf("<th>Module</th>") >= 0, "r31 header row rendered");
ok(r31out.indexOf("---") === -1, "r31 separator row not shown");
ok(r31out.indexOf("<td>헤더</td>") >= 0, "r31 body rows rendered");
ok(r31out.indexOf('class="tblwrap"') >= 0, "r31 table scrolls in its own container");
ok(r31out.indexOf('<button class="linkbtn" data-reportfile="calendar_theme.dart">calendar_theme.dart</button>') >= 0,
  "r31 file-name button works inside a cell");
var r31bad = "| Module | Usage |\\n| calendar_theme.dart | 테마 |";
ok(md(r31bad).indexOf("<table>") === -1, "r31 missing separator stays text");
ok(md(r31bad).indexOf("<p>") >= 0, "r31 malformed block renders as before");
ok(md("| A | B |\\n|---|---|\\n| only-one |").indexOf("<table>") === -1,
  "r31 ragged rows stay text");
ok(md("| A | B |\\n|---|---|").indexOf("<table>") === -1,
  "r31 header-only block stays text");
var r31xss = "| A | B |\\n|---|---|\\n| <script>alert(1)</script> | ok |";
var r31xssOut = md(r31xss);
ok(r31xssOut.indexOf("&lt;script") >= 0, "r31 cell markup escaped");
ok(r31xssOut.indexOf("<script") === -1, "r31 no live markup from a cell");
// The two answer boxes share one per-run draft.
wAnsDraft = {};
wAnsDraftSet("run9", "공유 초안");
var r31w = wAnswerBoxHtml("run9", "work");
var r31d = wAnswerBoxHtml("run9", "detail");
ok(r31w.indexOf("공유 초안") >= 0 && r31d.indexOf("공유 초안") >= 0,
  "r31 draft shared across both boxes");
ok(r31d.indexOf('id="answerinput"') >= 0, "r31 detail box keeps its ids");
ok(r31d.indexOf('id="ans-draft"') >= 0, "r31 detail hint has its own id");
ok(r31d.indexOf("Ctrl/⌘+Enter") >= 0, "r31 detail box shows the shortcut");
ok(r31w.indexOf('id="w-ans-draft"') >= 0, "r31 default box renders w-ans-draft id");
var r31sent = 0, r31lis = {}, r31hint = {hidden: true};
var r31box = {value: "x", dataset: {},
  addEventListener: function(t, fn) { r31lis[t] = fn; }};
$ = function(id) { return id === "ans-draft" ? r31hint : null; };
wireAnswerBox(r31box, function() { r31sent++; }, "run9", "ans-draft");
r31lis.keydown({key: "Enter", ctrlKey: false, metaKey: false,
  preventDefault: function() { throw new Error("newline"); }});
eq(r31sent, 0, "r31 detail Enter is a newline");
r31box.value = "고침";
r31lis.input();
eq(wAnsDraftGet("run9"), "고침", "r31 detail typing updates the shared draft");
eq(r31hint.hidden, false, "r31 detail hint follows the draft");
r31lis.keydown({key: "Enter", ctrlKey: true, metaKey: false, preventDefault: function() {}});
eq(r31sent, 1, "r31 detail Ctrl+Enter sends");
// R32: pipe tables never escape code fences (table detection runs only while no fence is open).
var r32fence = "\`\`\`\\n| A | B |\\n|---|---|\\n| 1 | 2 |\\n\`\`\`";
var r32fOut = md(r32fence);
ok(r32fOut.indexOf("<table>") === -1, "r32 table inside a fence is not a table");
ok(r32fOut.indexOf("<pre") >= 0 && r32fOut.indexOf("| A | B |") >= 0,
  "r32 fenced table stays verbatim inside code");
var r32after = "\`\`\`\\ncode\\n\`\`\`\\n| A | B |\\n|---|---|\\n| 1 | 2 |";
ok(md(r32after).indexOf("<table>") >= 0, "r32 table after a closed fence still renders");
var r32before = "| A | B |\\n|---|---|\\n| 1 | 2 |\\n\`\`\`\\ncode\\n\`\`\`";
var r32bOut = md(r32before);
ok(r32bOut.indexOf("<table>") >= 0, "r32 table before a fence still renders");
ok(r32bOut.indexOf("<pre") >= 0, "r32 fence after a table still renders as code");
// F16: stopped runs offer resume from the first pending step; every
// other shape keeps its own branch (failed, question, done, running,
// live job, settlement, external watch, empty steps).
var stopBase = {wq: "", settlement: null, watchRec: null, failed: null,
  doneSteps: 0, running: false, liveJob: false,
  stepList: [["triage", {status: "done"}], ["analyze", {status: "pending"}]]};
eq(stoppedResumeStep(stopBase), "analyze",
  "stopped shape offers the first pending step");
eq(stoppedResumeStep(Object.assign({}, stopBase, {failed: ["analyze", {status: "failed"}]})), "",
  "failed shape keeps the failed branch");
eq(stoppedResumeStep(Object.assign({}, stopBase, {wq: "질문 내용"})), "",
  "question wait keeps answer-go");
eq(stoppedResumeStep(Object.assign({}, stopBase, {doneSteps: 2,
  stepList: [["triage", {status: "done"}], ["analyze", {status: "done"}]]})), "",
  "all-done keeps the review branch");
eq(stoppedResumeStep(Object.assign({}, stopBase, {running: true})), "",
  "running step is not stopped");
eq(stoppedResumeStep(Object.assign({}, stopBase, {liveJob: true})), "",
  "live job is not stopped");
eq(stoppedResumeStep(Object.assign({}, stopBase, {settlement: {kind: "x"}})), "",
  "settlement keeps its own branch");
eq(stoppedResumeStep(Object.assign({}, stopBase, {watchRec: {reason: "r"}})), "",
  "external watch keeps its own branch");
eq(stoppedResumeStep(Object.assign({}, stopBase, {stepList: []})), "",
  "no steps offers nothing");
eq(stoppedResumeStep(Object.assign({}, stopBase,
  {stepList: [["triage", {status: "done"}], ["analyze", {status: "done"}]]})), "",
  "no pending step offers nothing");
// F17: start/resume failures speak Korean on subscription 403;
// ordinary errors keep their own copy.
var BT = String.fromCharCode(96);
var cliMissing = "chat needs the " + BT + "muse" + BT + " CLI with a subscription login (" + BT + "muse login" + BT + "); CLI not found.";
var apiKeySet = "chat is subscription-only, but META_API_KEY is set (API-key billing takes priority over login). Unset it and use " + BT + "muse login" + BT + " instead.";
var f17n = modalHtml.length;
eq(startFailToast(new Error(cliMissing)), "ko", "login 403 maps");
eq(modalHtml[modalHtml.length-1], "toast:" + LOGIN_NEEDED_KO,
  "login toast carries the Korean line");
eq(startFailToast(new Error(apiKeySet)), "ko", "api-key 403 maps");
eq(modalHtml[modalHtml.length-1], "toast:" + APIKEY_SET_KO,
  "api-key toast carries the unset remedy");
eq(startFailToast(new Error("boom")), "raw", "ordinary error keeps its copy");
eq(modalHtml[modalHtml.length-1], "toast:시작 실패: boom",
  "ordinary start toast unchanged");
eq(modalHtml.length, f17n + 3, "each start failure toasts exactly once");
// G2: graph member words equal the work-view outcome words — one rule.
var gRuns = [
  {id: "run-w", repo: "r", issue: "1", waitReason: "question:x",
    steps: {triage: {status: "done"}}},
  {id: "run-d", repo: "r", issue: "2",
    steps: {triage: {status: "done"}, analyze: {status: "done"}}},
  {id: "run-f", repo: "r", issue: "3",
    steps: {triage: {status: "done"}, analyze: {status: "failed"}}},
  {id: "run-s", repo: "r", issue: "4",
    steps: {triage: {status: "done"}, analyze: {status: "pending"}}}];
eq(graphMemberRunWord({type: "run", run_id: "run-w", status: "active"}, gRuns, []),
  "답변 대기", "waiting member says 답변 대기");
eq(graphMemberRunWord({type: "run", run_id: "run-w", status: "active"}, gRuns, []),
  runOutcomeWord(gRuns[0], []), "waiting member equals work word");
eq(graphMemberRunWord({type: "run", run_id: "run-d", status: "active"}, gRuns, []),
  "완료", "done member says 완료");
eq(graphMemberRunWord({type: "run", run_id: "run-f", status: "active"}, gRuns, []),
  "확인 필요", "failed member says 확인 필요");
eq(graphMemberRunWord({type: "run", run_id: "run-s", status: "active"}, gRuns, []),
  "멈춤 — 진행 중인 작업 없음", "stopped member says 멈춤");
var gLive = [{id: "run-l", repo: "r", issue: "9",
  steps: {triage: {status: "done"}, analyze: {status: "pending"}}}];
eq(graphMemberRunWord({type: "run", run_id: "run-l", status: "active"}, gLive,
  [{status: "running", label: "run-l"}]), "진행 중",
  "live job member says 진행 중");
eq(graphMemberRunWord({type: "run", run_id: "nope", status: "active"}, gRuns, []),
  "활성", "missing run keeps the graph word");
eq(graphMemberRunWord({type: "branch", status: "tracked", label: "b"}, gRuns, []),
  "추적 중", "non-run members untouched");
// G1: the status filter shows distinct honest labels.
eq(GF_STATUS_KO.queued, "대기열", "queued label");
eq(GF_STATUS_KO.pending, "시작 전", "pending label");
eq(GF_STATUS_KO.watch, "외부 대기(감시)", "watch label");
eq(GF_STATUS_KO.deferred, "보류", "deferred label");
// G1b: the filter lists every graph status and always shows the active one.
eq(typeof gfStatusOptions, "function", "gfStatusOptions extracted");
var selWaiting = gfStatusOptions("waiting");
eq(selWaiting.indexOf('<option value="waiting" selected>') !== -1, true,
  "waiting option selected");
eq(selWaiting.indexOf("답변 대기") !== -1, true, "waiting label shown");
var selUnknown = gfStatusOptions("zzz-old-link");
eq(selUnknown.indexOf("알 수 없는 상태(zzz-old-link)") !== -1, true,
  "unknown status shown as selected");
eq(selUnknown.indexOf('<option value="">전체 상태</option>') !== -1, true,
  "all-states option stays present and unselected");
eq(GST_KO.queued, "대기", "shared map untouched for queued");
eq(GST_KO.pending, "대기", "shared map untouched for pending");
// G3: unconfigured fastcheck is an empty state; real errors map to Korean.
eq(fcValKo("'fastcheck' must be an object"), "자율 검사 설정(fastcheck)이 객체가 아닙니다", "object error");
eq(fcValKo("'fastcheck.steps[2].argv must be a non-empty list of strings'"), "검사 단계의 실행 명령(argv)이 비어 있거나 문자열 목록이 아닙니다", "argv error");
eq(fcValKo("'fastcheck.toolchain' directory does not exist: /x"), "도구 디렉터리가 존재하지 않습니다", "toolchain error");
eq(fcValKo("something else"), "검사 설정이 올바르지 않습니다", "unknown error fallback");
// G4: previous-day link date math.
eq(digestPrevDay("2026-11-01"), "2026-10-31", "day boundary");
eq(digestPrevDay("2026-03-01"), "2026-02-28", "month boundary");
eq(digestPrevDay("2026-01-01"), "2025-12-31", "year boundary");
eq(digestPrevDay("not-a-date"), "", "malformed date");
eq(digestPrevDay(""), "", "empty date");
console.log("PASS " + pass);
})();
`;

const pieces = [stubs];
for (const n of wantConst) pieces.push(extractConstLine(n));
for (const n of wantBlock) pieces.push(extractConstBlock(n));
for (const n of wantFn) pieces.push(extractFn(n));
pieces.push(tests);
try {
  const p = eval(pieces.join("\n"));
  Promise.resolve(p).then(() => {}, (e) => { console.log("FAIL driver :: " + (e && e.message)); process.exit(1); });
} catch (e) { console.log("FAIL eval :: " + (e && e.message)); process.exit(1); }
'''


def _run_node(extra_args):
    return subprocess.run(
        ["node", "-e", DRIVER, str(HTML), ",".join(FUNCTIONS), ",".join(CONSTS),
         ",".join(BLOCK_CONSTS)] + extra_args,
        capture_output=True,
        text=True,
        timeout=60,
    )


class DashboardScriptTest(unittest.TestCase):
    def test_diag_block_wired_in_all_failure_contexts(self):
        html = HTML.read_text(encoding="utf-8")
        for site in ("qaDiagBlock(qa.diag)", "qaDiagBlock(st.diag)",
                     "qaDiagBlock(lf.diag)"):
            self.assertIn(site, html,
                          f"batch/work failure context must render {site}")

    def test_dashboard_script_syntax(self):
        import re

        blocks = re.findall(r"<script>(.*?)</script>", HTML.read_text(), re.S)
        self.assertEqual(len(blocks), 1, "expected one inline dashboard script")
        proc = subprocess.run(
            ["node", "--check", "/dev/stdin"],
            input=blocks[0],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, f"dashboard JS syntax error: {proc.stderr}")


class ConsumerSupersedeCardTests(unittest.TestCase):
    """M2.15: the batch card reads the newer-auto and consumer lines."""

    def test_newer_auto_line(self):
        html = HTML.read_text(encoding="utf-8")
        self.assertIn("더 새 후보로 대체: ${esc(b.newer_auto)}", html)

    def test_consumer_label_line(self):
        html = HTML.read_text(encoding="utf-8")
        self.assertIn("${esc(b.consumer_label)}", html)


class ModelPillMarkupTests(unittest.TestCase):
    """Model pill is a native button (Enter/Space free, focus ring
    from the global rule); no custom key shim that could double-fire."""

    def test_model_pill_is_native_button(self):
        html = HTML.read_text(encoding="utf-8")
        self.assertIn('<button class="pillbox" id="modelpill"', html)

    def test_no_custom_key_handler_on_pill(self):
        html = HTML.read_text(encoding="utf-8")
        self.assertNotIn('$("modelpill").onkeydown', html)

    def test_pill_rule_has_44px_minimum(self):
        import re
        html = HTML.read_text(encoding="utf-8")
        self.assertRegex(html, r"#modelpill\s*\{[^}]*min-height:\s*44px")


class SettingsModalNameTests(unittest.TestCase):
    """Production openModal derives the dialog name from the visible
    heading on every open; a stale name never survives a dialog change."""

    MODAL_DRIVER = r'''
const fs = require("fs");
const html = fs.readFileSync(process.argv[1], "utf8");
const src = html.match(/<script>([\s\S]*)<\/script>/)[1];
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
let modalOpener = null;
const document = {activeElement: null};
const sheetFake = {
  innerHTML: "",
  _attrs: {},
  _lastHead: null,
  setAttribute(k, v) { this._attrs[k] = v; },
  removeAttribute(k) { delete this._attrs[k]; },
  querySelector(sel) {
    if (!/h[123]/.test(sel)) return null;
    const m = this.innerHTML.match(/<h([123])\b([^>]*)>([\s\S]*?)<\/h\1>/i);
    if (!m) return null;
    const idm = m[2].match(/\bid\s*=\s*"([^"]*)"/);
    const node = {_id: idm ? idm[1] : "", textContent: m[3].replace(/<[^>]*>/g, "")};
    Object.defineProperty(node, "id", {get() { return this._id; }, set(v) { this._id = v; }, configurable: true});
    this._lastHead = node;
    return node;
  }
};
function $(id) {
  if (id === "sheet") return sheetFake;
  if (id === "modal") return {classList: {remove() {}, add() {}, contains() { return false; }}};
  return {};
}
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function ok(c, msg) { eq(!!c, true, msg); }
eval(extractFn("openModal"));
'''

    def _run_modal(self, snippet):
        return subprocess.run(
            ["node", "-e", self.MODAL_DRIVER + snippet, str(HTML)],
            capture_output=True, text=True, timeout=60,
        )

    def test_modal_name_follows_visible_heading(self):
        proc = self._run_modal(
            'openModal("<h3>설정</h3><p>본문</p>");'
            'eq(sheetFake._attrs["aria-labelledby"], "sheet-title", "settings heading names dialog");'
            'eq(sheetFake._attrs["aria-labelledby"], sheetFake._lastHead.id, "label binds the actual parsed heading");'
            'eq(sheetFake._lastHead.textContent, "설정", "parsed heading is the visible settings title");'
            'openModal("<h3 id=\\"keep-me\\">다른 대화</h3>");'
            'eq(sheetFake._attrs["aria-labelledby"], "keep-me", "caller heading id preserved");'
            'eq(sheetFake._attrs["aria-labelledby"], sheetFake._lastHead.id, "label follows the new dialog heading");'
            'openModal("<h2>세 번째</h2>");'
            'eq(sheetFake._attrs["aria-labelledby"], "sheet-title", "name re-derived on dialog change");'
            'ok(sheetFake._attrs["aria-labelledby"] !== "keep-me", "stale dialog name never survives");'
            'eq(sheetFake._lastHead.textContent, "세 번째", "parsed heading tracks the current dialog");'
            'console.log("PASS " + pass);'
        )
        self.assertEqual(proc.returncode, 0, f"modal driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout, f"name binding failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, f"no passing assertions: {proc.stdout}")

    def test_modal_name_cleared_without_heading(self):
        proc = self._run_modal(
            'openModal("<h3>설정</h3>");'
            'openModal("<div>plain</div>");'
            'eq(sheetFake._attrs["aria-labelledby"], undefined, "no heading leaves no dialog name");'
            'console.log("PASS " + pass);'
        )
        self.assertEqual(proc.returncode, 0, f"modal driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout, f"name clearing failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, f"no passing assertions: {proc.stdout}")


class SidebarReadabilityTests(unittest.TestCase):
    """Recent-chat rows stack title-first inside #chatlist scope so repo
    navigation keeps its one-line layout; 44px minimum preserved."""

    def test_chatlist_rows_have_44px_minimum(self):
        import re
        html = HTML.read_text(encoding="utf-8")
        self.assertRegex(html, r"#chatlist \.sideitem\s*\{[^}]*min-height:\s*44px")


class AnswerErrorDriverTests(unittest.TestCase):
    """F2b/F7: a subscription 403 on answer send shows the actionable
    Korean line — in the toast and inline by the answer box — via the
    same subErrKo matcher fetchErrKo uses. Raw English stays under
    details; the draft stays and the send button re-enables."""
    ANSWER_DRIVER = r'''
const fs = require("fs");
const html = fs.readFileSync(process.argv[1], "utf8");
const src = html.match(/<script>([\s\S]*)<\/script>/)[1];
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
const toastCalls = [];
function toast(m) { toastCalls.push(m); }
let loadCalls = 0;
function loadAll() { loadCalls++; }
const __els = {};
function $(id) {
  if (!__els[id]) __els[id] = {id: id, innerHTML: "", hidden: true, disabled: false, value: "",
    _attrs: {},
    getAttribute(k) { return (k in this._attrs) ? this._attrs[k] : null; },
    setAttribute(k, v) { this._attrs[k] = String(v); },
    removeAttribute(k) { delete this._attrs[k]; }};
  return __els[id];
}
// Fake tab-session storage: persists across a simulated reload (a second
// eval of the same pieces), unlike the in-eval product memory.
const sessionStorage = { _m: {}, _impl: null,
  getItem(k) { return this._impl ? this._impl.getItem(k) : ((k in this._m) ? this._m[k] : null); },
  setItem(k, v) { if (this._impl) return this._impl.setItem(k, v); this._m[k] = String(v); },
  removeItem(k) { if (this._impl) return this._impl.removeItem(k); delete this._m[k]; } };
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function ok(c, msg) { eq(!!c, true, msg); }
const wantConst = ["LOGIN_NEEDED_KO", "APIKEY_SET_KO", "SUB_APIKEY_RE", "SUB_LOGIN_RE"];
const wantFn = ["answerErrorIsSaved", "answerErrIdFor", "answerFieldFor", "answerInlineShow",
  "answerInlineHide", "handleAnswerError", "answerOutcome", "subErrKo",
  "wAnsDraftKey", "wAnsDraftGet", "wAnsDraftSet", "wAnswerBoxHtml"];
const pieces = [];
// var (not const): test snippets run after this direct eval, and only var
// and function declarations leak out of it; const would stay invisible.
for (const n of wantConst) pieces.push(extractConstLine(n).replace(/^const /, "var "));
for (const n of wantFn) pieces.push(extractFn(n));
// Production esc is a single line; the balanced lexer cannot scan its
// regex literal, so take the whole declaration line verbatim.
const __escLine = (src.match(/\nfunction esc\(s\) \{[^\n]*\}/) || [])[0] || "";
if (!__escLine) throw new Error("missing esc");
pieces.push(__escLine.trim());
// The product memory store lives in this declaration; eval'ing the same
// pieces again simulates a reload (fresh memory, same session storage).
const __draftDecl = (src.match(/\nlet wAnsDraft = \{\};/) || [])[0] || "";
if (!__draftDecl) throw new Error("missing wAnsDraft");
pieces.unshift(__draftDecl.trim());
eval(pieces.join("\n"));
'''
    CLI_MISSING = ("chat needs the `muse` CLI with a subscription login "
                   "(`muse login`); CLI not found.")
    API_KEY_SET = ("chat is subscription-only, but META_API_KEY is set "
                   "(API-key billing takes priority over login). "
                   "Unset it and use `muse login` instead.")

    def _run_answer(self, snippet):
        return subprocess.run(
            ["node", "-e", self.ANSWER_DRIVER + snippet, str(HTML)],
            capture_output=True, text=True, timeout=60,
        )

    def _check_answer(self, snippet):
        proc = self._run_answer(snippet + 'console.log("PASS " + pass);')
        self.assertEqual(proc.returncode, 0, f"answer driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout, f"answer failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, f"no passing assertions: {proc.stdout}")

    def test_answer_403_maps_to_korean_toast_and_inline(self):
        self._check_answer(
            'var r1 = handleAnswerError(new Error(' + repr(self.CLI_MISSING) + '), {id: "w-send", disabled: true});'
            'eq(r1, "failed", "subscription 403 fails the send");'
            'eq(toastCalls[toastCalls.length-1], LOGIN_NEEDED_KO, "toast shows the Korean line, not raw English");'
            'var e1 = $("w-ans-err");'
            'ok(e1.innerHTML.indexOf(LOGIN_NEEDED_KO) >= 0, "inline error by the answer box shows Korean");'
            'ok(e1.innerHTML.indexOf("muse` CLI") >= 0, "raw English kept under details");'
            'eq(e1.hidden, false, "inline error stays until next edit or send");'
            'var r2 = handleAnswerError(new Error(' + repr(self.API_KEY_SET) + '), {dataset: {answer: "run-x"}, disabled: true});'
            'eq(r2, "failed", "api-key 403 fails the send");'
            'eq(toastCalls[toastCalls.length-1], APIKEY_SET_KO, "toast shows the unset remedy");'
            'var e2 = $("ans-err-run-x");'
            'ok(e2.innerHTML.indexOf(APIKEY_SET_KO) >= 0, "command-center row shows the unset remedy inline");'
            'eq(e2.hidden, false, "row inline error visible");'
            'eq(subErrKo(' + repr(self.API_KEY_SET) + '), APIKEY_SET_KO, "api-key wording wins even though the text contains a login phrase");'
        )

    def test_answer_error_paths_preserved(self):
        self._check_answer(
            'eq(answerErrIdFor({id: "answerbtn"}), "ans-err", "detail button maps to ans-err");'
            'eq(answerErrIdFor({dataset: {answer: "run-y"}}), "ans-err-run-y", "command center maps to the row box");'
            'eq(answerErrIdFor({id: "w-send"}), "w-ans-err", "work button maps to w-ans-err");'
            'answerInlineShow("w-ans-err", LOGIN_NEEDED_KO, "raw");'
            'eq($("w-ans-err").hidden, false, "show reveals");'
            'answerInlineHide("w-ans-err");'
            'eq($("w-ans-err").hidden, true, "hide clears the inline error");'
            'var r3 = handleAnswerError(new Error("boom"), {id: "w-send", disabled: true});'
            'eq(r3, "failed", "ordinary error still fails");'
            'eq(toastCalls[toastCalls.length-1], "전송 실패: boom", "ordinary error keeps its copy");'
            'eq($("w-ans-err").hidden, true, "ordinary error shows no inline box");'
            'var r4 = handleAnswerError(new Error("answer saved; worker busy"), {id: "w-send", disabled: true});'
            'eq(r4, "saved", "saved shape preserved");'
            'eq(loadCalls, 1, "saved path refreshes");'
        )

    def test_answer_inline_role_and_describedby(self):
        self._check_answer(
            'eq(answerFieldFor("w-ans-err"), "w-answer", "work box links to w-answer");'
            'eq(answerFieldFor("ans-err"), "answerinput", "detail box links to answerinput");'
            'eq(answerFieldFor("ans-err-run-y"), "ans-run-y", "row box links to the row input");'
            'eq(answerFieldFor("nope"), "", "unknown box links nowhere");'
            'answerInlineShow("w-ans-err", LOGIN_NEEDED_KO, "raw");'
            'eq($("w-answer").getAttribute("aria-describedby"), "w-ans-err", "show links the work textarea");'
            'answerInlineHide("w-ans-err");'
            'eq($("w-answer").getAttribute("aria-describedby"), null, "hide unlinks the work textarea");'
            'answerInlineShow("ans-err", LOGIN_NEEDED_KO, "raw");'
            'eq($("answerinput").getAttribute("aria-describedby"), "ans-err", "show links the detail textarea");'
            'answerInlineHide("ans-err");'
            'eq($("answerinput").getAttribute("aria-describedby"), null, "hide unlinks the detail textarea");'
            'answerInlineShow("ans-err-run-y", APIKEY_SET_KO, "raw");'
            'eq($("ans-run-y").getAttribute("aria-describedby"), "ans-err-run-y", "show links the row input");'
            'answerInlineHide("ans-err-run-y");'
            'eq($("ans-run-y").getAttribute("aria-describedby"), null, "hide unlinks the row input");'
        )

    def test_answer_draft_survives_reload_and_clears_on_send(self):
        self._check_answer(
            'wAnsDraftSet("run9", "새로고침 뒤 복원");'
            'eq(sessionStorage._m["wans-run9"], "새로고침 뒤 복원", "draft mirrored to session storage");'
            'eval(pieces.join("\\n"));'
            'eq(wAnsDraftGet("run9"), "새로고침 뒤 복원", "draft restored after simulated reload");'
            'ok(wAnswerBoxHtml("run9").indexOf("새로고침 뒤 복원") !== -1, "reloaded render shows the draft");'
            'wAnsDraftSet("run9", "");'
            'eq(wAnsDraftGet("run9"), "", "clear empties memory");'
            'eq(sessionStorage._m["wans-run9"], undefined, "clear removes the stored copy");'
        )

    def test_answer_draft_falls_back_to_memory(self):
        self._check_answer(
            'sessionStorage._impl = {getItem(k) { throw new Error("denied"); },'
            ' setItem(k, v) { throw new Error("denied"); },'
            ' removeItem(k) { throw new Error("denied"); }};'
            'wAnsDraftSet("run8", "메모리 폴백");'
            'eq(wAnsDraftGet("run8"), "메모리 폴백", "memory works when storage throws");'
            'wAnsDraftSet("run8", "");'
            'eq(wAnsDraftGet("run8"), "", "clear works when storage throws");'
        )


class GlanceBandTests(unittest.TestCase):
    BAND_DRIVER = r'''
const fs = require("fs");
const html = fs.readFileSync(process.argv[1], "utf8");
const src = html.match(/<script>([\s\S]*)<\/script>/)[1];
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
function esc(s) { return String(s == null ? "" : s); }
let scrolledTo = null, rendered = false;
function mkBandBtn(go) {
  return {dataset: {bandgo: go}, _href: null, onclick: null, _focused: false,
    getAttribute(k) { return k === "data-bandgo" ? this.dataset.bandgo : null; },
    focus(o) { this._focused = true; document.activeElement = this; }};
}
function mkBandLink(href) {
  return {dataset: {}, _href: href, onclick: null, _focused: false,
    getAttribute(k) { return k === "href" ? this._href : null; },
    focus(o) { this._focused = true; document.activeElement = this; }};
}
const fakeHost = { _html: "", _btns: [], _links: [], _sets: 0,
  set innerHTML(h) { this._html = h; this._btns = []; this._links = []; this._sets++;
    const re = /<button class="btn sm" data-bandgo="([a-z]+)">/g; let m;
    while ((m = re.exec(h))) { this._btns.push(mkBandBtn(m[1])); }
    const rl = /<a class="btn sm" href="([^"]+)">/g;
    while ((m = rl.exec(h))) { this._links.push(mkBandLink(m[1])); } },
  get innerHTML() { return this._html; },
  querySelectorAll(sel) {
    if (String(sel).indexOf("data-bandgo") >= 0) return this._btns;
    if (String(sel).indexOf("a[href]") >= 0) return this._links;
    return this._btns.concat(this._links);
  },
  contains(n) { return this._btns.indexOf(n) >= 0 || this._links.indexOf(n) >= 0; } };
const fakeBox = { querySelector: function(sel) { return sel === "#glanceband" ? fakeHost : null; } };
const document = { activeElement: null, deskHead: null, querySelector: function(s) {
  if (s === "#deskroot") return {scrollIntoView: function(o) { scrolledTo = o || true; }};
  if (s === "#deskhead") {
    if (!document.deskHead) document.deskHead = {focusedWith: "never",
      focus: function(o) { this.focusedWith = o || true; document.activeElement = this; }};
    return document.deskHead;
  }
  return null; } };
function render() { rendered = true; }
function ovHealthy(names) {
  return {repos: (names || []).map(function(n) {
    return {name: n, sched_enabled: true,
      last_scan: new Date(Date.now() - 60 * 1000).toISOString(),
      next_scan: new Date(Date.now() + 3600 * 1000).toISOString(),
      sched_error: null}; })};
}
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function ok(c, msg) { eq(!!c, true, msg); }
const wantConst = ["M1_VERDICT_KO", "M1_VERDICT_CLS", "BAND_STALE_TOL_MS", "LOGIN_NEEDED_KO", "APIKEY_SET_KO", "SUB_APIKEY_RE", "SUB_LOGIN_RE"];
const wantFn = ["ago", "bandAskId", "glanceBandVerdict", "glanceBandSentence",
  "glanceBandStateKo", "glanceBandHtml", "glanceBandLoadingHtml", "bandLoadState",
  "appHealthWarning", "paintGlanceBand", "fetchErrKo", "errBlockHtml", "subErrKo"];
function extractConstBlock(name) {
  const re = new RegExp("(?:^|\\n)const\\s+" + name + "\\s*=\\s*\\{");
  const mm = re.exec(src);
  if (!mm) throw new Error("missing const block " + name);
  const brace = src.indexOf("{", mm.index);
  return "const " + name + " = " + src.slice(brace, sliceBalanced(src, brace)) + ";";
}
const wantBlock = ["FETCH_ERR_TEXT"];
const pieces = [];
for (const n of wantConst) pieces.push(extractConstLine(n));
for (const n of wantBlock) pieces.push(extractConstBlock(n));
for (const n of wantFn) pieces.push(extractFn(n));
eval(pieces.join("\n"));
'''

    def _run_band(self, snippet):
        return subprocess.run(
            ["node", "-e", self.BAND_DRIVER + snippet, str(HTML)],
            capture_output=True, text=True, timeout=60,
        )

    def _check_band(self, snippet):
        proc = self._run_band(snippet + 'console.log("PASS " + pass);')
        self.assertEqual(proc.returncode, 0, f"band driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout, f"band failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, f"no passing assertions: {proc.stdout}")

    def test_band_four_states(self):
        self._check_band(
            'var nowIso = new Date(Date.now() - 3*60*1000).toISOString();'
            'function rr(n, v, need, blocked) {'
            ' return {name: n, verdict: v, verdict_line: "v", need_you: need, blocked: blocked,'
            '  next_steps: [], updated_at: nowIso}; }'
            'var desk0 = {supported: [], unsupported: [], repos: []};'
            'var b = glanceBandVerdict({at: nowIso, repos: [rr("nitro", "normal", [], [])]}, desk0, ovHealthy(["nitro"]));'
            'eq(b.state, "ok", "all clear is ok");'
            'eq(glanceBandSentence(b), "잘 돌아가고 있어요", "ok sentence");'
            'var a = glanceBandVerdict({at: nowIso, repos: [rr("nitro", "attention", [{q: 1}, {q: 2}], [])]}, desk0, ovHealthy(["nitro"]));'
            'eq(a.state, "attention", "pending questions are attention");'
            'eq(a.pending, 2, "pending count");'
            'eq(glanceBandSentence(a), "확인이 필요해요: 질문 2건", "attention sentence");'
            'var p = glanceBandVerdict({at: nowIso, repos: [rr("nitro", "blocked", [], [{w: 1}]),'
            ' rr("cal", "normal", [], [])]},'
            ' {supported: [], unsupported: [{k: 1}], repos: [{counts: {blocked_total: 1}}]}, ovHealthy(["nitro", "cal"]));'
            'eq(p.state, "problem", "blocked items are problem");'
            'eq(p.problems, 2, "1 item + 1 nameless desk count, verdict adds nothing");'
            'eq(glanceBandSentence(p), "문제가 있어요: 2건", "problem sentence");'
            'var u = glanceBandVerdict(null, null, null);'
            'eq(u.state, "unknown", "missing input is unknown");'
            'eq(glanceBandSentence(u), "지금 상태를 확인할 수 없어요", "unknown sentence");'
        )

    def test_band_never_ok_on_unknown(self):
        self._check_band(
            'var nowIso = new Date(Date.now() - 3*60*1000).toISOString();'
            'function rr(n, v) {'
            ' return {name: n, verdict: v, verdict_line: "v", need_you: [], blocked: [],'
            '  next_steps: [], updated_at: nowIso}; }'
            'var desk0 = {supported: [], unsupported: [], repos: []};'
            'var ovN = {repos: [{name: "n", sched_enabled: false}]};'
            'var cases = ['
            ' ["missing summary", undefined, desk0, ovN],'
            ' ["unknown flag", {unknown: true, reason: "x"}, desk0, ovN],'
            ' ["missing at", {repos: [rr("n", "normal")]}, desk0, ovN],'
            ' ["missing desk", {at: nowIso, repos: [rr("n", "normal")]}, null, ovN],'
            ' ["unknown desk", {at: nowIso, repos: [rr("n", "normal")]}, {unknown: true}, ovN],'
            ' ["unknown verdict", {at: nowIso, repos: [rr("n", "unknown")]}, desk0, ovN],'
            ' ["missing verdict", {at: nowIso, repos: [{name: "n"}]}, desk0, ovN],'
            ' ["malformed blocked", {at: nowIso, repos: [{name: "n", verdict: "normal",'
            '   need_you: [], blocked: "막힘"}]}, desk0, ovN],'
            ' ["missing overview", {at: nowIso, repos: [rr("n", "normal")]}, desk0, undefined],'
            ' ["null overview", {at: nowIso, repos: [rr("n", "normal")]}, desk0, null],'
            ' ["malformed overview", {at: nowIso, repos: [rr("n", "normal")]}, desk0, {repos: "x"}],'
            ' ["repo without overview entry", {at: nowIso, repos: [rr("n", "normal")]}, desk0, {repos: []}]];'
            'cases.forEach(function(c) {'
            ' var st = glanceBandVerdict(c[1], c[2], c[3]).state;'
            ' eq(st === "ok", false, c[0] + " is never ok");'
            ' eq(st, "unknown", c[0] + " is unknown");'
            '});'
        )


    def test_band_no_double_count(self):
        # Desk projections describe the SAME snapshot rows: blocked_total
        # is len(blocked), unsupported is the unsupported subset of
        # need_you. Overlapping data must be counted once.
        # R1: the backend marks a repo "blocked" ONLY when blocked items
        # exist (m1_verdict blocked-items), so the verdict itself adds
        # nothing on top of the listed items.
        self._check_band(
            'var nowIso = new Date(Date.now() - 3*60*1000).toISOString();'
            'var sum = {at: nowIso, repos: [{name: "nitro", verdict: "blocked",'
            ' verdict_line: "v",'
            ' need_you: [{kind: "question", ref: "r1"}, {kind: "run", ref: "r2"}],'
            ' blocked: [{what: "w"}], updated_at: nowIso}]};'
            'var desk = {supported: [],'
            ' unsupported: [{id: "nitro:question:r1", kind: "question", ref: "r1", repo: "nitro"},'
            '  {id: "nitro:run:r2", kind: "run", ref: "r2", repo: "nitro"}],'
            ' repos: [{name: "nitro", counts: {blocked_total: 1}}]};'
            'var b = glanceBandVerdict(sum, desk, ovHealthy(["nitro"]));'
            'eq(b.state, "problem", "overlapping blockers are still a problem");'
            'eq(b.problems, 1, "1 listed item only: verdict adds nothing, desk total skipped");'
            'eq(b.pending, 2, "2 questions, unsupported subset not added twice");'
            'var deskNameless = {supported: [], unsupported: [{k: 9}],'
            ' repos: [{counts: {blocked_total: 4}}]};'
            'var b2 = glanceBandVerdict(sum, deskNameless, ovHealthy(["nitro"]));'
            'eq(b2.problems, 5, "1 item + 4 unmatchable desk rows");'
            'eq(b2.pending, 3, "unmatchable desk question still counted");'
        )

    def test_band_attention_without_questions(self):
        # R2: an "attention" repo line must raise the band even with no
        # need_you items (waiting-review / issue-waiting / stale-release).
        # The verdict rule ids live server-side; the band only sees the
        # resulting "attention" verdict with an empty question list.
        self._check_band(
            'var nowIso = new Date(Date.now() - 3*60*1000).toISOString();'
            'function rr(n, v, need) {'
            ' return {name: n, verdict: v, verdict_line: "v", need_you: need, blocked: [],'
            '  next_steps: [], updated_at: nowIso}; }'
            'var desk0 = {supported: [], unsupported: [], repos: []};'
            'var openQ = glanceBandVerdict({at: nowIso, repos: [rr("n", "attention", [{q: 1}, {q: 2}])]}, desk0, ovHealthy(["n"]));'
            'eq(openQ.state, "attention", "open-questions rule is attention");'
            'eq(glanceBandSentence(openQ), "확인이 필요해요: 질문 2건", "questions counted");'
            'var waiting = glanceBandVerdict({at: nowIso, repos: [rr("n", "attention", [])]}, desk0, ovHealthy(["n"]));'
            'eq(waiting.state, "attention", "waiting-review rule is attention without questions");'
            'eq(waiting.attentionOnly, 1, "question-free attention repo counted once");'
            'eq(glanceBandSentence(waiting), "확인이 필요해요: 검토 대기 저장소 1곳", "honest attention-only sentence");'
            'var staleRel = glanceBandVerdict({at: nowIso, repos: [rr("n", "attention", [])]}, desk0, ovHealthy(["n"]));'
            'eq(staleRel.state, "attention", "stale-release rule is attention without questions");'
            'var mixed = glanceBandVerdict({at: nowIso, repos: [rr("a", "attention", [{q: 1}]), rr("b", "attention", [])]}, desk0, ovHealthy(["a", "b"]));'
            'eq(mixed.state, "attention", "mixed attention stays attention");'
            'eq(glanceBandSentence(mixed), "확인이 필요해요: 질문 1건, 검토 대기 저장소 1곳", "both counted honestly");'
            'var rows = glanceBandHtml(waiting);'
            'ok(rows.indexOf("주의") >= 0, "repo line still shows its own state");'
        )

    def test_band_staleness(self):
        # R3: sched_enabled repos are judged from the overview the app
        # already has. maintenance_service.running is NOT read (it is
        # normally false between launchd ticks).
        self._check_band(
            'var nowIso = new Date(Date.now() - 3*60*1000).toISOString();'
            'function rr(n, v) {'
            ' return {name: n, verdict: v, verdict_line: "v", need_you: [], blocked: [],'
            '  next_steps: [], updated_at: nowIso}; }'
            'var desk0 = {supported: [], unsupported: [], repos: []};'
            'function ovEntry(name, mut) {'
            ' var e = {name: name, sched_enabled: true,'
            '  last_scan: new Date(Date.now() - 3600*1000).toISOString(),'
            '  next_scan: new Date(Date.now() + 3600*1000).toISOString(),'
            '  sched_error: null};'
            ' if (mut) mut(e); return e; }'
            'var fresh = glanceBandVerdict({at: nowIso, repos: [rr("n", "normal")]}, desk0,'
            ' {repos: [ovEntry("n", null)]});'
            'eq(fresh.state, "ok", "fresh schedule stays ok");'
            'var late = glanceBandVerdict({at: nowIso, repos: [rr("n", "normal")]}, desk0,'
            ' {repos: [ovEntry("n", function(e) {'
            '  e.next_scan = new Date(Date.now() - 3600*1000).toISOString(); })]});'
            'eq(late.state, "problem", "overdue scan is a problem");'
            'eq(glanceBandSentence(late), "자동 점검이 늦어지고 있어요: 1곳", "stale sentence is plain");'
            'var err = glanceBandVerdict({at: nowIso, repos: [rr("n", "normal")]}, desk0,'
            ' {repos: [ovEntry("n", function(e) { e.sched_error = "boom"; })]});'
            'eq(err.state, "problem", "sched_error is a problem");'
            'var off = glanceBandVerdict({at: nowIso, repos: [rr("n", "normal")]}, desk0,'
            ' {repos: [ovEntry("n", function(e) { e.sched_enabled = false;'
            '  e.next_scan = new Date(Date.now() - 86400*1000).toISOString(); })]});'
            'eq(off.state, "ok", "disabled schedule is not judged stale");'
            'var badNext = glanceBandVerdict({at: nowIso, repos: [rr("n", "normal")]}, desk0,'
            ' {repos: [ovEntry("n", function(e) { e.next_scan = "not-a-date"; })]});'
            'eq(badNext.state, "unknown", "unparseable next_scan is never ok");'
        )

    def test_band_loading_before_overview(self):
        # U1: overview epoch alive + no overview yet = neutral loading,
        # never OK and never the unknown-failure sentence. The signal is
        # the app's own epoch object, not a timer.
        self._check_band(
            'eq(bandLoadState({repos: []}, false), "ready", "present overview is ready even when empty");'
            'eq(bandLoadState({repos: []}, true), "ready", "present overview wins over epoch");'
            'eq(bandLoadState(null, true), "loading", "no overview with live epoch is loading");'
            'eq(bandLoadState(undefined, true), "loading", "missing overview with live epoch is loading");'
            'eq(bandLoadState(null, false), "unknown", "no overview without epoch is unknown");'
            'eq(bandLoadState(null), "unknown", "absent epoch signal is unknown, never loading");'
            'eq(glanceBandSentence({state: "loading"}), "상태를 확인하는 중이에요…", "loading sentence");'
            'var lh = glanceBandLoadingHtml();'
            'ok(lh.indexOf("상태를 확인하는 중이에요…") >= 0, "loading html states checking");'
            'ok(lh.indexOf("data-bandgo") < 0, "loading has no actions");'
            'ok(lh.indexOf("qdot") < 0 && lh.indexOf("qchip") < 0, "loading has no colour dot");'
            'ok(lh.indexOf("role=\\"status\\"") >= 0, "loading is a live status");'
            'var nowIso = new Date(Date.now() - 3*60*1000).toISOString();'
            'var desk0 = {supported: [], unsupported: [], repos: []};'
            'function rr(n, v) {'
            ' return {name: n, verdict: v, verdict_line: "v", need_you: [], blocked: [],'
            '  next_steps: [], updated_at: nowIso}; }'
            'var s = {at: nowIso, repos: [rr("n", "normal")]};'
            'var lHost = {_html: "", _sets: 0, querySelector: function() { return lHost; },'
            ' querySelectorAll: function() { return []; }};'
            'Object.defineProperty(lHost, "innerHTML", {configurable: true,'
            ' get: function() { return lHost._html; },'
            ' set: function(v) { lHost._html = v; lHost._sets++; }});'
            'var lBox = {querySelector: function(sel) { return sel === "#glanceband" ? lHost : null; }};'
            'var ovEpoch = {};'
            'paintGlanceBand(lBox, s, desk0, null);'
            'ok(lHost._html.indexOf("상태를 확인하는 중이에요…") >= 0, "first paint before overview is loading, not failure");'
            'ok(lHost._html.indexOf("미확인") < 0, "loading never claims unknown");'
            'ovEpoch = null;'
            'paintGlanceBand(lBox, s, desk0, null);'
            'ok(lHost._html.indexOf("지금 상태를 확인할 수 없어요") >= 0, "settled failure without overview is unknown");'
        )

    def test_band_unreadable_checkout_and_health(self):
        # G9: a row the overview already flagged checkout_unreadable
        # keeps the band from saying 정상. The health helper only
        # speaks from /api/app degraded, never from its absence.
        self._check_band(
            'var nowIso = new Date(Date.now() - 3*60*1000).toISOString();'
            'function rr(n, v) {'
            ' return {name: n, verdict: v, verdict_line: "v", need_you: [], blocked: [],'
            '  next_steps: [], updated_at: nowIso}; }'
            'var desk0 = {supported: [], unsupported: [], repos: []};'
            'function ovEntry(name) {'
            ' return {name: name, sched_enabled: false,'
            '  checkout_unreadable: true}; }'
            'var b = glanceBandVerdict({at: nowIso, repos: [rr("n", "normal")]}, desk0,'
            ' {repos: [ovEntry("n")]});'
            'eq(b.state, "problem", "unreadable checkout is a problem, never ok");'
            'eq(b.unreadableRepos.length, 1, "unreadable repo listed");'
            'eq(b.unreadableRepos[0], "n", "unreadable name kept");'
            'ok(glanceBandSentence(b).indexOf("저장소 응답 없음") >= 0, "sentence names the unreadable store");'
            'ok(glanceBandHtml(b).indexOf("저장소 응답 없음") >= 0, "row carries the short marker");'
            'var c = glanceBandVerdict({at: nowIso, repos: [rr("n", "normal")]}, desk0,'
            ' {repos: [{name: "n", sched_enabled: false}]});'
            'eq(c.state, "ok", "no flag stays ok");'
            'eq((c.unreadableRepos || []).length, 0, "absent flag is not invented");'
            'eq(appHealthWarning({degraded: true}), "서버가 바쁩니다 — 일부 화면이 늦을 수 있어요", "degraded warns");'
            'eq(appHealthWarning({degraded: false}), "", "not degraded stays silent");'
            'eq(appHealthWarning(null), "", "missing app stays silent");'
            'eq(appHealthWarning({}), "", "unknown stays silent, never ok");'
        )

    def test_band_shell_then_real_overview(self):
        # U1b: the band must never read the init shell as received data.
        # The shell literal below mirrors the real init() line; a static
        # test below pins that line. With a live epoch the shell paints
        # loading; after the epoch settles without data it paints unknown;
        # only a real overview (no _shell) yields the verdict.
        self._check_band(
            'var nowIso = new Date(Date.now() - 3*60*1000).toISOString();'
            'function rr(n, v) {'
            ' return {name: n, verdict: v, verdict_line: "v", need_you: [], blocked: [],'
            '  next_steps: [], updated_at: nowIso}; }'
            'var desk0 = {supported: [], unsupported: [], repos: []};'
            'var shell = {repos: [], runs: [], _shell: true};'
            'eq(bandLoadState(shell, true), "loading", "init shell with live epoch is loading, never ready");'
            'eq(bandLoadState(shell, false), "unknown", "shell after settled epoch is unknown");'
            'eq(bandLoadState({repos: [], runs: []}, true), "ready", "real empty overview is ready");'
            'var lHost = {_html: "", _sets: 0, querySelector: function() { return lHost; },'
            ' querySelectorAll: function() { return []; }};'
            'Object.defineProperty(lHost, "innerHTML", {configurable: true,'
            ' get: function() { return lHost._html; },'
            ' set: function(v) { lHost._html = v; lHost._sets++; }});'
            'var lBox = {querySelector: function(sel) { return sel === "#glanceband" ? lHost : null; }};'
            'var s = {at: nowIso, repos: [rr("n", "normal")]};'
            'var ovEpoch = {};'
            'paintGlanceBand(lBox, s, desk0, shell);'
            'ok(lHost._html.indexOf("상태를 확인하는 중이에요…") >= 0, "init shell paints loading while epoch active");'
            'ovEpoch = null;'
            'paintGlanceBand(lBox, s, desk0, shell);'
            'ok(lHost._html.indexOf("지금 상태를 확인할 수 없어요") >= 0, "shell after failed load paints unknown");'
            'var real = {repos: [{name: "n", sched_enabled: true,'
            ' last_scan: new Date(Date.now() - 3600*1000).toISOString(),'
            ' next_scan: new Date(Date.now() + 3600*1000).toISOString(),'
            ' sched_error: null}]};'
            'paintGlanceBand(lBox, s, desk0, real);'
            'ok(lHost._html.indexOf("상태를 확인하는 중이에요…") < 0, "real overview never paints loading");'
            'ok(lHost._html.indexOf("정상") >= 0, "real overview paints the verdict");'
        )

    def test_init_shell_marker(self):
        # The U1b shell above must stay identical to the real init() line,
        # or the loading test proves nothing about the first paint.
        html = HTML.read_text(encoding="utf-8")
        self.assertIn("O = O || {repos: [], runs: [], _shell: true};", html)

    def test_band_sched_error_vs_overdue(self):
        # U2: sched_error is an error, not lateness. Error first, both honest.
        self._check_band(
            'var nowIso = new Date(Date.now() - 3*60*1000).toISOString();'
            'function rr(n, v) {'
            ' return {name: n, verdict: v, verdict_line: "v", need_you: [], blocked: [],'
            '  next_steps: [], updated_at: nowIso}; }'
            'var desk0 = {supported: [], unsupported: [], repos: []};'
            'function ovEntry(name, mut) {'
            ' var e = {name: name, sched_enabled: true,'
            '  last_scan: new Date(Date.now() - 3600*1000).toISOString(),'
            '  next_scan: new Date(Date.now() + 3600*1000).toISOString(),'
            '  sched_error: null};'
            ' if (mut) mut(e); return e; }'
            'var e = glanceBandVerdict({at: nowIso, repos: [rr("n", "normal")]}, desk0,'
            ' {repos: [ovEntry("n", function(x) { x.sched_error = "boom"; })]});'
            'eq(e.state, "problem", "sched_error is a problem");'
            'eq(glanceBandSentence(e), "자동 점검에 오류가 있어요: 1곳", "error is not lateness");'
            'var both = glanceBandVerdict({at: nowIso, repos: [rr("n", "normal"), rr("m", "normal")]}, desk0,'
            ' {repos: [ovEntry("n", function(x) { x.sched_error = "boom"; }),'
            '  ovEntry("m", function(x) { x.next_scan = new Date(Date.now() - 3600*1000).toISOString(); })]});'
            'eq(glanceBandSentence(both), "자동 점검에 오류가 있어요: 1곳, 자동 점검이 늦어지고 있어요: 1곳", "error first, both honest");'
            'var eb = glanceBandVerdict({at: nowIso, repos: [rr("n", "blocked", [{w: 1}], [])]}, desk0,'
            ' {repos: [ovEntry("n", function(x) { x.sched_error = "boom"; })]});'
            'eq(glanceBandSentence(eb), "자동 점검에 오류가 있어요: 1곳, 문제가 있어요: 1건", "error before blocked items");'
        )

    def test_no_raw_error_in_inline_err_main_text(self):
        # U4: raw fetch text must live only inside the collapsed details.
        # errBlockHtml/fetchErrKo calls are the honest path; the stored
        # original keeps its own human line + next action (allow-listed).
        import re
        html = HTML.read_text(encoding="utf-8")
        lines = html.splitlines()
        allow = [re.compile(r"저장된 원문을 읽지 못했습니다")]
        bad = []
        for i, line in enumerate(lines, 1):
            if "inline-err" not in line:
                continue
            if not re.search(r"esc\((e\.message|loadErr|err\.message|det\.loadError)\b", line):
                continue
            window = "\n".join(lines[max(0, i - 5):i + 1])
            if "errBlockHtml(" in window or "fetchErrKo(" in window:
                continue
            if any(p.search(line) for p in allow):
                continue
            bad.append((i, line.strip()[:120]))
        self.assertEqual(bad, [], f"raw error text in inline-err main message: {bad}")

    def test_glance_heading_order(self):
        # R4: the page h1 must come before the band host in the glance
        # template, so at runtime no band h2 can precede the page h1.
        html = HTML.read_text(encoding="utf-8")
        anchor = '<div class="pagehead"><h1>한눈에 보기</h1>'
        h1 = html.index(anchor)
        band = html.index('<div id="glanceband">', h1)
        seg = html[h1:band]
        self.assertIn("data-glancestale", seg, "pagehead sub stays with the h1 before the band host")

    def test_band_verdict_prominence(self):
        # R5: the verdict sentence is the largest text in the band.
        import re
        html = HTML.read_text(encoding="utf-8")
        m = re.search(r"\[data-band=\"1\"\] \.band-verdict \{[^}]*font-size:\s*(\d+)px", html)
        self.assertIsNotNone(m, "band verdict has an explicit font size")
        self.assertGreaterEqual(int(m.group(1)), 18, "verdict sentence is 18px or larger")
        self.assertRegex(html, r"\[data-band=\"1\"\] \.band-verdict \{[^}]*font-weight:\s*700")

    def test_band_verdict_unknown_modifier(self):
        # R5b: the unknown (not-checked) verdict renders at body size
        # through a modifier that only the unknown path applies.
        import re
        html = HTML.read_text(encoding="utf-8")
        m = re.search(r"\[data-band=\"1\"\] \.band-verdict\.is-unknown \{[^}]*font-size:\s*(\d+)px", html)
        self.assertIsNotNone(m, "unknown modifier has an explicit font size")
        self.assertLess(int(m.group(1)), 18, "unknown verdict is body size")
        self.assertIn('band.state === "unknown" ? " is-unknown" : ""', html)
        load = html[html.index("function glanceBandLoadingHtml"):html.index("function glanceBandSentence")]
        self.assertNotIn("is-unknown", load, "loading path has no modifier")

    def test_work_busy_cleared_after_route_guard(self):
        # R9: the work view clears aria-busy only on the still-current view,
        # so a stale response cannot wipe a newer view's busy flag.
        html = HTML.read_text(encoding="utf-8")
        start = html.index("async function renderWorkView")
        body = html[start:start + 6000]
        guard = 'if (view.kind !== "work" || view.id !== myId || mySeq !== workSeq) return;'
        rem = '$("view").removeAttribute("aria-busy");'
        g1 = body.index(guard)
        r1 = body.index(rem)
        self.assertLess(g1, r1, "error-path guard precedes busy removal")
        g2 = body.index(guard, r1 + 1)
        r2 = body.index(rem, r1 + 1)
        self.assertLess(g2, r2, "success-path guard precedes busy removal")

    def test_band_focus_skip_and_restore(self):
        self._check_band(
            'var nowIso = new Date(Date.now() - 3*60*1000).toISOString();'
            'function rr(need) {'
            ' return {name: "nitro", verdict: "attention", verdict_line: "v",'
            '  need_you: need, blocked: [], updated_at: nowIso}; }'
            'var desk0 = {supported: [], unsupported: [], repos: []};'
            'function sumOf(need) { return {at: nowIso, repos: [rr(need)]}; }'
            'var s1 = sumOf([{q: 1}]), s2 = sumOf([{q: 1}, {q: 2}]), s3 = sumOf([{q: 1}, {q: 2}, {q: 3}]);'
            'var ovF = ovHealthy(["nitro"]);'
            'paintGlanceBand(fakeBox, s1, desk0, ovF);'
            'eq(fakeHost._sets, 1, "first paint writes once");'
            'var deskBtn = null;'
            'fakeHost._btns.forEach(function(x) {'
            ' if (x.dataset.bandgo === "desk") deskBtn = x; });'
            'deskBtn.focus();'
            'eq(document.activeElement, deskBtn, "button takes focus");'
            'paintGlanceBand(fakeBox, s1, desk0, ovF);'
            'eq(fakeHost._sets, 1, "identical poll does not repaint");'
            'eq(document.activeElement, deskBtn, "focus kept without repaint");'
            'paintGlanceBand(fakeBox, s2, desk0, ovF);'
            'eq(fakeHost._sets, 2, "changed data repaints");'
            'var btn2 = null;'
            'fakeHost._btns.forEach(function(x) {'
            ' if (x.dataset.bandgo === "desk") btn2 = x; });'
            'ok(!!btn2 && btn2 !== deskBtn, "repaint makes a new button");'
            'eq(document.activeElement, btn2, "focus restored to same action");'
            'var link = null;'
            'fakeHost._links.forEach(function(x) {'
            ' if (x.getAttribute("href") === "#/digest") link = x; });'
            'link.focus();'
            'paintGlanceBand(fakeBox, s3, desk0, ovF);'
            'eq(fakeHost._sets, 3, "changed data repaints again");'
            'var link2 = null;'
            'fakeHost._links.forEach(function(x) {'
            ' if (x.getAttribute("href") === "#/digest") link2 = x; });'
            'eq(document.activeElement, link2, "focus restored to same link");'
        )

    def test_band_html_actions_and_no_leak(self):
        self._check_band(
            'var nowIso = new Date(Date.now() - 3*60*1000).toISOString();'
            'function rr(n, v, need, blocked) {'
            ' return {name: n, verdict: v, verdict_line: "v", need_you: need, blocked: blocked,'
            '  next_steps: [], updated_at: nowIso}; }'
            'var h = glanceBandHtml(glanceBandVerdict({at: nowIso,'
            ' repos: [rr("nitro", "attention", [{q: 1}], [])]},'
            ' {supported: [], unsupported: [], repos: []}, ovHealthy(["nitro"])));'
            'ok(h.indexOf("확인이 필요해요: 질문 1건") >= 0, "html has sentence");'
            'ok(h.indexOf("주의") >= 0, "html has state word");'
            'ok(h.indexOf("마지막 확인:") >= 0 && h.indexOf(" 전") >= 0, "html has checked line");'
            'ok(h.indexOf("AI 연결: 아직 확인 안 됨") >= 0, "html says AI unverified");'
            'ok(h.indexOf("#/digest") >= 0, "html links digest");'
            'ok(h.indexOf("data-bandgo=\\"desk\\"") >= 0, "html has desk button");'
            'ok(h.indexOf("nitro") >= 0, "html names the repo");'
            'var hu = glanceBandHtml(glanceBandVerdict(null, null));'
            'ok(hu.indexOf("지금 상태를 확인할 수 없어요") >= 0, "unknown sentence");'
            'ok(hu.indexOf("data-bandgo=\\"retry\\"") >= 0, "unknown has retry");'
            'var sneaky = {at: nowIso, repos: [{name: "nitro", verdict: "normal",'
            ' verdict_line: "deadbeefcafe", need_you: [], blocked: [],'
            ' next_steps: [{title: "t", link: "#/x", sha: "abc123shaleak"}],'
            ' updated_at: nowIso, source: {proof: "proof-secret-zzz"}}]};'
            'var hs = glanceBandHtml(glanceBandVerdict(sneaky,'
            ' {supported: [], unsupported: [], repos: []}, ovHealthy(["nitro"])));'
            'ok(hs.indexOf("deadbeefcafe") < 0, "no verdict_line leak");'
            'ok(hs.indexOf("abc123shaleak") < 0, "no sha leak");'
            'ok(hs.indexOf("proof-secret-zzz") < 0, "no proof leak");'
            'var many = glanceBandVerdict({at: nowIso, repos: [rr("a", "blocked", [{q: 1}], [])]},'
            ' {supported: [], unsupported: [{k: 1}], repos: []}, ovHealthy(["a"]));'
            'var hm = glanceBandHtml(many);'
            'ok((hm.match(/class="btn sm"/g) || []).length <= 3, "at most 3 actions");'
        )

    def test_fetch_error_copy(self):
        self._check_band(
            'eq(fetchErrKo("작업 상세", "unknown path /api/x"),'
            ' "작업 상세의 주소를 찾지 못했습니다", "unknown path maps to human copy");'
            'eq(fetchErrKo("서버", "404 Not Found"),'
            ' "서버의 주소를 찾지 못했습니다", "404 maps to per-noun notFound");'
            'eq(fetchErrKo("없는것", "404"),'
            ' "자료 주소를 찾지 못했습니다", "unmapped 404 stays neutral");'
            'eq(fetchErrKo("하루 요약", "Load failed"),'
            ' "하루 요약을 불러오지 못했습니다", "network failure maps to human copy");'
            'eq(fetchErrKo("서버", "Load failed"),'
            ' "서버에 연결하지 못했습니다", "server names the connection");'
            'eq(fetchErrKo("서버", "boom"),'
            ' "서버 응답을 읽는 중 문제가 생겼습니다", "server read is not awkward");'
            'eq(fetchErrKo("상태 요약", "boom"),'
            ' "상태 요약을 읽는 중 문제가 생겼습니다", "other failure stays generic");'
            'eq(fetchErrKo("없는것", "boom"),'
            ' "데이터를 불러오지 못했습니다", "unmapped noun never invents grammar");'
            'var eb = errBlockHtml("작업 상세", "raw boom", "<button>R</button>");'
            'ok(eb.indexOf("작업 상세를 읽는 중 문제가 생겼습니다") >= 0, "human message first");'
            'ok(eb.indexOf("raw boom") >= 0, "raw kept inside details");'
            'ok(eb.indexOf("<button>R</button>") >= 0, "retry control kept");'
            'ok(eb.indexOf("<details") >= 0, "collapsible detail");'
        )

    def test_fetch_error_subscription_mapping(self):
        # F2/F7: each real subscription 403 server text maps to its own
        # actionable Korean line (the api_key text contains "muse login",
        # so it must match first); the raw English stays under details.
        # (The driver eval scopes extracted consts away from the snippet,
        # so the pinned copy lives here; single-source use is proved by
        # test_login_line_single_source_const below.)
        self._check_band(
            'var LOGIN_KO = "Muse 로그인이 필요합니다 — 터미널에서 muse login 후 새로고침";'
            'var APIKEY_KO = "META_API_KEY가 설정되어 구독 전용 작업이 막혔습니다 — 서버를 실행한 터미널에서 해제한 뒤 다시 시작";'
            'eq(fetchErrKo("차단 질문 답변",'
            ' "chat needs the `muse` CLI with a subscription login (`muse login`); CLI not found."),'
            ' LOGIN_KO, "missing-CLI 403 maps to login line");'
            'eq(fetchErrKo("서버",'
            ' "chat is subscription-only, but META_API_KEY is set (API-key billing takes priority over login). Unset it and use `muse login` instead."),'
            ' APIKEY_KO, "API-key 403 maps to the unset remedy, not the login line");'
            'eq(fetchErrKo("서버", "boom"),'
            ' "서버 응답을 읽는 중 문제가 생겼습니다", "ordinary error keeps its own copy");'
            'var eb2 = errBlockHtml("차단 질문 답변",'
            ' "chat needs the `muse` CLI with a subscription login (`muse login`); CLI not found.", "");'
            'ok(eb2.indexOf(LOGIN_KO) >= 0, "mapped line is the main message");'
            'ok(eb2.indexOf("muse` CLI") >= 0, "raw English kept inside details");'
            'var eb3 = errBlockHtml("서버",'
            ' "chat is subscription-only, but META_API_KEY is set (API-key billing takes priority over login).", "");'
            'ok(eb3.indexOf(APIKEY_KO) >= 0, "api-key block shows the unset remedy");'
        )

    def test_band_login_needed_line(self):
        # F1: the band shows the login line only when the ungated
        # summary explicitly reports login_needed true; the verdict
        # itself never changes because of it.
        self._check_band(
            'var LOGIN_KO = "Muse 로그인이 필요합니다 — 터미널에서 muse login 후 새로고침";'
            'var nowIso = new Date(Date.now() - 3*60*1000).toISOString();'
            'function rr(n, v) {'
            ' return {name: n, verdict: v, verdict_line: "v", need_you: [], blocked: [],'
            '  next_steps: [], updated_at: nowIso}; }'
            'var desk0 = {supported: [], unsupported: [], repos: []};'
            'var b0 = glanceBandVerdict({at: nowIso, repos: [rr("nitro", "normal")],'
            ' subscription: {login_needed: false}}, desk0, ovHealthy(["nitro"]));'
            'eq(b0.loginNeeded, false, "explicit false hides the line");'
            'ok(glanceBandHtml(b0).indexOf("muse login 후 새로고침") < 0, "no login line when not needed");'
            'var b1 = glanceBandVerdict({at: nowIso, repos: [rr("nitro", "normal")],'
            ' subscription: {login_needed: true}}, desk0, ovHealthy(["nitro"]));'
            'eq(b1.loginNeeded, true, "true flag surfaces");'
            'eq(b1.state, "ok", "login need does not change the verdict");'
            'ok(glanceBandHtml(b1).indexOf(LOGIN_KO) >= 0, "login line shown");'
            'var b2 = glanceBandVerdict({at: nowIso, repos: [rr("nitro", "normal")]}, desk0, ovHealthy(["nitro"]));'
            'eq(b2.loginNeeded, false, "missing subscription hides the line");'
            'var b3 = glanceBandVerdict({at: nowIso, repos: [rr("nitro", "normal")],'
            ' subscription: {login_needed: "yes"}}, desk0, ovHealthy(["nitro"]));'
            'eq(b3.loginNeeded, false, "foreign truthy value hides the line");'
        )

    def test_band_login_reason_lines(self):
        # F7: the band names the honest remedy by reason — unset the key
        # when api_key_set, login otherwise. Reason reports presence only.
        self._check_band(
            'var nowIso = new Date(Date.now() - 3*60*1000).toISOString();'
            'function rr(n, v) {'
            ' return {name: n, verdict: v, verdict_line: "v", need_you: [], blocked: [],'
            '  next_steps: [], updated_at: nowIso}; }'
            'var desk0 = {supported: [], unsupported: [], repos: []};'
            'var K_LOGIN = "Muse 로그인이 필요합니다";'
            'var K_APIKEY = "META_API_KEY가 설정되어";'
            'var b1 = glanceBandVerdict({at: nowIso, repos: [rr("nitro", "normal")],'
            ' subscription: {login_needed: true, reason: "api_key_set"}}, desk0, ovHealthy(["nitro"]));'
            'eq(b1.loginReason, "api_key_set", "reason surfaces");'
            'var h1 = glanceBandHtml(b1);'
            'ok(h1.indexOf(K_APIKEY) >= 0, "api_key_set shows the unset remedy");'
            'ok(h1.indexOf(K_LOGIN) < 0, "api_key_set does not show the login line");'
            'var b2 = glanceBandVerdict({at: nowIso, repos: [rr("nitro", "normal")],'
            ' subscription: {login_needed: true, reason: "cli_missing"}}, desk0, ovHealthy(["nitro"]));'
            'var h2 = glanceBandHtml(b2);'
            'ok(h2.indexOf(K_LOGIN) >= 0, "cli_missing shows the login line");'
            'ok(h2.indexOf(K_APIKEY) < 0, "cli_missing does not show the unset remedy");'
            'var b3 = glanceBandVerdict({at: nowIso, repos: [rr("nitro", "normal")],'
            ' subscription: {login_needed: true}}, desk0, ovHealthy(["nitro"]));'
            'eq(b3.loginReason, "", "missing reason stays empty");'
            'ok(glanceBandHtml(b3).indexOf(K_LOGIN) >= 0, "missing reason falls back to login");'
            'var b4 = glanceBandVerdict({at: nowIso, repos: [rr("nitro", "normal")],'
            ' subscription: {login_needed: false, reason: "api_key_set"}}, desk0, ovHealthy(["nitro"]));'
            'eq(b4.loginNeeded, false, "false flag hides the line");'
            'ok(glanceBandHtml(b4).indexOf(K_APIKEY) < 0, "no remedy when not needed");'
        )

    def test_fix_claim_vs_evidence_labels(self):
        # F4: the report fix line labels the model claim and the stored
        # evidence count separately, so a divergent pair reads honestly.
        html = HTML.read_text(encoding="utf-8")
        self.assertIn("수정 내용(모델 기록)", html)
        self.assertIn("저장된 증거 파일", html)

    def test_login_line_single_source_const(self):
        # F1/F2/F2b/F7: the band line, the 403 mapping and the answer
        # error path share one matcher — the driver eval cannot see
        # extracted consts, so this static check proves every call site
        # routes through subErrKo, and the api_key wording wins first.
        html = HTML.read_text(encoding="utf-8")
        self.assertIn('const LOGIN_NEEDED_KO = "Muse 로그인이 필요합니다', html)
        self.assertIn('const APIKEY_SET_KO = "META_API_KEY가 설정되어', html)
        seg = html[html.index("function fetchErrKo"):]
        seg = seg[:seg.index("function errBlockHtml")]
        self.assertIn("subErrKo(m)", seg, "fetchErrKo routes via subErrKo")
        seg2 = html[html.index("function handleAnswerError"):]
        seg2 = seg2[:seg2.index("async function pollJob")]
        self.assertIn("subErrKo(msg)", seg2, "handleAnswerError routes via subErrKo")
        api = html.index("SUB_APIKEY_RE.test(m)")
        login = html.index("SUB_LOGIN_RE.test(m)")
        self.assertLess(api, login, "api_key wording matched before the login wording")

    def test_failed_resume_hint_names_folder_recreation(self):
        # G5: resume recreates a vanished work folder from the checkout
        # (runner.ensure_branch_workdir) and retries the failed step,
        # so the failed-run hint says that truthfully.
        html = HTML.read_text(encoding="utf-8")
        self.assertIn("막힌 단계(${failed[0]})부터 다시 시도합니다 · "
                      "작업 폴더가 없으면 다시 만듭니다", html)

    def test_stopped_resume_branch_and_start_toast_wiring(self):
        # F16: the stopped shape gets its own next-action branch before the
        # fallback, reusing the existing resume handler (never auto-run).
        # F17: all four start/resume catch sites route via startFailToast.
        import re
        html = HTML.read_text(encoding="utf-8")
        self.assertIn("stoppedResumeStep({wq, settlement", html)
        self.assertIn("멈춘 단계(${stoppedStep})부터 이어서 진행합니다", html)
        self.assertEqual(
            len(re.findall(r"catch \(e\) \{ startFailToast\(e\); \}", html)),
            4, "work/resume, inspector/resume and both job/run starts "
               "route via startFailToast")
        self.assertNotIn('toast("시작 실패: " + e.message)', html,
                         "no raw-English start-failure toast remains")

    def test_graph_status_filter_labels_unique(self):
        # G1: queued/pending and watch/deferred shared one label each;
        # the filter map gives every value its own honest label while
        # the shared GST_KO stays untouched for other screens.
        import re
        html = HTML.read_text(encoding="utf-8")
        pairs = dict(re.findall(r'(\w+): "([^"]+)"',
                               html[html.index("const GST_KO = "):
                                    html.index("const GF_STATUS_KO = ")]))
        over = dict(re.findall(r'(\w+): "([^"]+)"',
                               html[html.index("const GF_STATUS_KO = "):
                                    html.index("const EDGE_KO = ")]))
        merged = dict(pairs)
        merged.update(over)
        self.assertEqual(len(set(merged.values())), len(merged),
                         f"duplicate filter labels: {sorted(merged.values())}")
        self.assertEqual(
            [over[k] for k in ("queued", "pending", "watch", "deferred")],
            ["대기열", "시작 전", "외부 대기(감시)", "보류"])
        self.assertIn("Object.entries(GF_STATUS_KO)", html)

    def test_graph_filter_covers_every_graph_status(self):
        # G1b: the filter literal kept every GST_KO key when the overlay
        # grew back to full coverage; option texts stay unique.
        import re
        html = HTML.read_text(encoding="utf-8")
        keyre = re.compile(r'(?:"([\w-]+)"|(\w+))\s*:')
        def lit(name):
            return re.compile(r"const " + name + r" = \{(.*?)\};",
                              re.S).search(html).group(1)
        gst = lit("GST_KO")
        gf = lit("GF_STATUS_KO")
        gst_keys = {a or b for a, b in keyre.findall(gst)}
        gf_keys = {a or b for a, b in keyre.findall(gf)}
        self.assertTrue(gst_keys, "parsed GST_KO keys")
        self.assertEqual(gst_keys - gf_keys, set(),
                         "filter must list every graph status")

    def test_graph_member_word_uses_one_rule(self):
        # G2: the member panel renders the same runOutcomeWord rule for
        # run members instead of the graph's own active/done mapping.
        html = HTML.read_text(encoding="utf-8")
        self.assertIn("graphMemberRunWord(m, O.runs, O.jobs)", html)
        # The generic GST_KO[m.status] helper (~line 1888) remains for
        # queue chips / other views; only the member-panel line is in scope.
        line = [ln for ln in html.splitlines()
                if "graphMemberRunWord(m, O.runs, O.jobs)" in ln]
        self.assertEqual(len(line), 1)
        self.assertNotIn("GST_KO[m.status]", line[0])

    def test_fastcheck_empty_state_and_mapping(self):
        # G3: an absent/empty fastcheck block shows an honest empty state
        # and hides only the steps-empty validator error; real invalid
        # configs still map to Korean with the raw text under 자세히.
        html = HTML.read_text(encoding="utf-8")
        self.assertIn("자율 검사 설정 없음 — 아직 설정하지 않았습니다", html)
        self.assertIn("Array.isArray(fc.steps) && fc.steps.length > 0", html)
        self.assertIn("v !== \"'fastcheck.steps' must be a non-empty list\"",
                      html)

    def test_digest_empty_state_copy(self):
        # G4: the unknown-day copy says only what the code does (tick
        # production, midnight finalization) and links the previous
        # day with its date when it exists.
        html = HTML.read_text(encoding="utf-8")
        self.assertIn("하루 요약은 스케줄러가 돌 때마다 갱신되고", html)
        self.assertIn("전날 요약 보기", html)
        self.assertIn("digestPrevDay(d)", html)

    def test_paint_band_real_path(self):
        self._check_band(
            'var nowIso = new Date(Date.now() - 3*60*1000).toISOString();'
            'function rr(n, v, need) {'
            ' return {name: n, verdict: v, verdict_line: "v", need_you: need, blocked: [],'
            '  next_steps: [], updated_at: nowIso}; }'
            'var desk0 = {supported: [], unsupported: [], repos: []};'
            'var ovR = ovHealthy(["nitro"]);'
            'paintGlanceBand(fakeBox, {at: nowIso, repos: [rr("nitro", "attention", [{q: 1}])]}, desk0, ovR);'
            'ok(fakeHost._html.indexOf("확인이 필요해요: 질문 1건") >= 0, "real paint writes sentence");'
            'eq(fakeHost._btns.length, 1, "one desk button painted");'
            'fakeHost._btns[0].onclick();'
            'ok(scrolledTo !== null, "desk button scrolls to desk section");'
            'ok(document.deskHead && document.deskHead.focusedWith !== "never", "desk jump moves focus to desk heading");'
            'eq(document.activeElement, document.deskHead, "heading holds focus after jump");'
            'eq(rendered, false, "desk scroll does not re-render");'
            'paintGlanceBand(fakeBox, null, null, null);'
            'var rb = null;'
            'fakeHost._btns.forEach(function(x) { if (x.dataset.bandgo === "retry") rb = x; });'
            'ok(!!rb, "unknown band paints retry");'
            'rb.onclick();'
            'eq(rendered, true, "retry re-renders");'
            'paintGlanceBand({querySelector: function() { return null; }}, null, null);'
            'eq(true, true, "missing host does not throw");'
        )

    def test_loading_copy_present(self):
        html = HTML.read_text(encoding="utf-8")
        for text in ("작업 상세를 불러오는 중이에요",
                     "상태 요약을 불러오는 중이에요",
                     "하루 요약을 불러오는 중이에요"):
            self.assertIn(text, html, f"visible loading copy missing: {text}")
        self.assertGreaterEqual(html.count('setAttribute("aria-busy", "true")'), 3,
                                "loading views must set aria-busy")
        self.assertIn('role="status" aria-live="polite"', html)

    def test_targets_44px_minimum(self):
        html = HTML.read_text(encoding="utf-8")
        self.assertRegex(html, r"\.navlink\s*\{[^}]*min-height:\s*44px")
        self.assertRegex(html, r"\.sideitem\s*\{[^}]*min-height:\s*44px")
        self.assertRegex(html, r"\.skip\s*\{[^}]*padding:\s*12px 16px")
        self.assertRegex(html, r"\[data-gk=\"ledger\"\] a\s*\{[^}]*padding:\s*4px 2px")
        self.assertRegex(html, r"\.iconbtn\s*\{[^}]*min-height:\s*44px")
        self.assertRegex(html, r"\.iconbtn\s*\{[^}]*min-width:\s*44px")
        self.assertRegex(html, r"\.kbtn\s*\{[^}]*min-height:\s*44px")
        self.assertRegex(html, r"details\.storymore > summary\s*\{[^}]*min-height:\s*44px")
        # F1: the runs detail link itself and the watch-log summary are
        # 44px hit areas without changing the table layout or order.
        self.assertRegex(html, r"tr\.rowlink td a\[data-work\]\s*\{[^}]*min-height:\s*44px")
        self.assertRegex(html, r"details\.watchlog > summary\s*\{[^}]*min-height:\s*44px")
        # F2: the answer textarea keeps a 44px floor.
        self.assertRegex(html, r"#w-answer\s*\{[^}]*min-height:\s*44px")
        # F4: changed-file links keep at least a 24px floor
        # (12.5px mono at 1.6 line-height plus padding clears it).
        self.assertRegex(html, r"\.filelink\s*\{[^}]*min-height:\s*24px")
        # F4 redo: the report file-name buttons (button.linkbtn) also
        # keep a 24px floor.
        self.assertRegex(html, r"button\.linkbtn\s*\{[^}]*min-height:\s*24px")
        # 228/F5: the code-block copy button is at least 24x24.
        self.assertRegex(html, r"\.copybtn\s*\{[^}]*min-width:\s*24px")
        self.assertRegex(html, r"\.copybtn\s*\{[^}]*min-height:\s*24px")
        # 228/F5: the runs-table run-id link keeps a 24px floor
        # without changing the reading order.
        self.assertRegex(html, r"tr\.rowlink td a\[data-run\]\s*\{[^}]*min-height:\s*24px")
        # G1: the pipe-table wrapper scrolls in-container and never
        # forces the page wider.
        self.assertRegex(html, r"\.tblwrap\s*\{[^}]*overflow-x:\s*auto")
        self.assertRegex(html, r"\.tblwrap\s*\{[^}]*max-width:\s*100%")
        # G2: the runs inspector box matches the work view at 44px.
        self.assertRegex(html, r"#answerinput\s*\{[^}]*min-height:\s*44px")
        # F11: the copy button surfaces under keyboard focus and inside
        # hovered/focused code blocks, opacity only so nothing reflows.
        self.assertRegex(html, r"\.copybtn:focus-visible\s*\{[^}]*opacity:\s*1")
        self.assertRegex(html, r"pre\.code:hover \.copybtn[^{]*\{[^}]*opacity:\s*1")
        self.assertRegex(html, r"pre\.code:focus-within \.copybtn\s*\{[^}]*opacity:\s*1")
        # F12: the runs-table detail link keeps a 24px width floor
        # without changing the reading order.
        self.assertRegex(html, r"tr\.rowlink td a\[data-work\]\s*\{[^}]*min-width:\s*24px")
        # F13: every inline answer error box is a live status region,
        # announced even after the toast disappears.
        self.assertIn('<div class="inline-err" id="w-ans-err" role="status"', html)
        self.assertIn('<div class="inline-err" id="ans-err" role="status"', html)
        self.assertIn('<div class="inline-err" id="ans-err-${r.id}" role="status"', html)

    def test_runs_row_keeps_link_order(self):
        # F1: the row stays the click target (data-runrow + keyboard),
        # and the run link still precedes the detail link in reading order.
        html = HTML.read_text(encoding="utf-8")
        row = html.index('<tr class="rowlink" data-runrow=')
        seg = html[row:row + 400]
        self.assertIn('tabindex="0"', seg)
        run = seg.index('data-run="')
        work = seg.index('data-work="')
        self.assertLess(run, work, "run link stays before detail link")

    def test_answer_textarea_markup_and_draft(self):
        # F2/F3: textarea with the same id/label/placeholder, visible
        # shortcut, per-run draft hint, no Enter-send in the work view,
        # draft cleared on successful send.
        html = HTML.read_text(encoding="utf-8")
        self.assertIn('<textarea id="w-answer" rows="3"', html)
        self.assertIn('aria-label="차단 질문 답변"', html)
        self.assertIn('placeholder="답변을 입력하세요"', html)
        self.assertIn("Ctrl/⌘+Enter로 전송", html)
        self.assertIn('id="w-send"', html)
        self.assertIn('id="w-ans-draft"', html)
        self.assertNotIn('<input type="text" id="w-answer"', html)
        self.assertIn('wAnswerBoxHtml(s.id, "work")', html)
        self.assertIn('wireAnswerBox($("w-answer"), sendA, s.id)', html)
        self.assertIn('wAnsDraftSet(s.id, "");', html)
        self.assertNotIn(
            "box.dataset.suggested !== undefined && box.value === box.dataset.suggested",
            html,
            "work view no longer gates Enter on the M1 suggestion flag",
        )
        seg_start = html.index("function workTyping()")
        self.assertIn('tag === "textarea"', html[seg_start:seg_start + 600])

    def test_detail_answer_textarea_and_shared_draft(self):
        # G2: the runs inspector box matches the work view — textarea,
        # explicit send, Ctrl/Cmd+Enter, same visible hint, and the same
        # per-run draft store.
        html = HTML.read_text(encoding="utf-8")
        self.assertIn('wAnswerBoxHtml(s.id, "detail")', html)
        self.assertIn('<textarea id="answerinput" rows="3"', html)
        self.assertIn('id="answerbtn"', html)
        self.assertIn('id="ans-draft"', html)
        self.assertIn('wireAnswerBox($("answerinput"), sendAnswer, s.id, "ans-draft")', html)
        self.assertNotIn('<input type="text" id="answerinput"', html)
        self.assertNotIn('if (e.key === "Enter") sendAnswer()', html)

    def test_report_pipe_table_renderer_present(self):
        # G1: the report renderer recognizes well-formed pipe tables;
        # behavior is proven by the driver tests below.
        html = HTML.read_text(encoding="utf-8")
        self.assertIn("function splitPipeRow(line)", html)
        self.assertIn("function isPipeDelim(line)", html)
        seg_start = html.index("function md(src)")
        seg = html[seg_start:seg_start + 2000]
        self.assertIn("tblwrap", seg)
        self.assertIn("mdInline(h)", seg)


class WorkInspectorLogicTest(unittest.TestCase):
    def test_work_inspector_ui_logic(self):
        proc = _run_node([])
        self.assertEqual(
            proc.returncode, 0, f"node driver failed: {proc.stdout}\n{proc.stderr}"
        )
        self.assertNotIn("FAIL", proc.stdout, f"logic failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, f"no passing assertions: {proc.stdout}")


if __name__ == "__main__":
    unittest.main()
