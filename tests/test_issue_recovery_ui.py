"""Recovery UI: shipped helper semantics and blocked-card behavior.

Offline Node driver extracts the ACTUAL rec*/paintRecoverySection code
from dashboard.html (no copies). jget/jpost/pollJob/toast/window are
stubbed; no network, no product data, no model. Real 390px/keyboard
poll acceptance stays a later browser gate.
"""
import ast
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

CAND = Path(__file__).resolve().parent.parent
HTML = CAND / "src" / "openmainty" / "dashboard.html"


def server_hyp_defs():
    """Canonical server-owned hypothesis meanings, read from the
    actual product source (AST, never imported or executed).

    The UI choice labels must equal these exact server strings: the
    browser sends only the opaque id, so any drift between the table
    and the rendered labels breaks the worker's prompt. Copying the
    literals into the test would not catch that drift.
    """
    src = (CAND / "src" / "openmainty" / "derived_fix.py").read_text(
        encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_HYPOTHESIS_DEFS"
                for t in node.targets):
            defs = ast.literal_eval(node.value)
            assert set(defs) == {"H-A", "H-B"}, defs
            return defs
    raise AssertionError("server _HYPOTHESIS_DEFS not found")
FUNCTIONS = ["recConflictedPaths", "recOriginalTouched",
             "recHasPreparedSource", "recFollowupAllowed",
             "recFastcheckTruth", "ownerDecisionBlock",
             "paintRecoverySection",
             "paintPrepFollowups", "paintDerivedFixSection",
             "pfBlockedShort", "pfSummaryTitle", "pfLeadHtml",
             "pfRunBlock", "dfixAuthorityNote",
             "pfWorkerProfileHtml"]

TRACKBOX_JS = r"""
/* Tracked-box harness: innerHTML replacement invalidates prior
   nodes like a real DOM. Every query parses CURRENT markup, so a
   stale external boolean or a detached button can never fake panel
   or wiring state. record collects focus events for intent
   assertions. No network, no product data. */
function __parseAttrs(tag) {
  const out = {};
  const re = /([\w-]+)="([^"]*)"/g;
  let m;
  while ((m = re.exec(tag))) out[m[1]] = m[2];
  return out;
}
function __mkTrackBox(record) {
  /* One render generation owns one connected node set: every query
     within the generation returns the SAME element objects, so
     product-wired onclick handlers persist across queries exactly
     like a real DOM. innerHTML replacement disconnects the old set
     (isConnected false, like detached nodes) and builds a fresh one
     parsed from the CURRENT markup — a stale object can never
     mutate the new generation. focus() always logs the product
     call but moves activeElement only for connected nodes, exactly
     like a real DOM where detached focus is a state no-op. */
  let html = "";
  let gen = 0;
  let cache = null;
  const box = {dataset: {}, _dFixInputSel: null,
    _dFixInputLoading: null, _dFixMoved: null, _dFixAcked: null,
    _dFixRetry: null, _dFixClear: false, _dFixSubmitting: false,
    contains(el) { return !!el && el.isConnected === true && el.__gen === gen; },
    get innerHTML() { return html; },
    set innerHTML(v) {
      detach();
      html = String(v); gen++; cache = null;
      try {
        const d = (typeof globalThis !== "undefined" && globalThis.document) || null;
        const ae = d && d.activeElement;
        if (ae && typeof ae.__gen === "number" && ae.__gen !== gen)
          d.activeElement = (d && d.body) || null;
      } catch (e) {}
    }};
  function tag(el) { el.__gen = gen; el.isConnected = true; return el; }
  function detach() {
    if (!cache) return;
    const all = [cache.note, cache.panel, cache.summary, cache.form]
      .concat(cache.opens, cache.closes, cache.cancels, cache.radios);
    for (const el of all) if (el) el.isConnected = false;
  }
  function doFocus(el, label) {
    return () => {
      record.push(label);
      if (el.isConnected === true) {
        try {
          const d = (typeof globalThis !== "undefined" && globalThis.document) || null;
          if (d) d.activeElement = el;
        } catch (e) {}
      }
    };
  }
  function build() {
    /* Every node is parsed from CURRENT markup; initial open and
       checked states come from the markup itself. Panel open is
       per-element live state: toggling the current object is what
       the product rereads, and a detached object can never reach
       the next generation. */
    const live = {opens: [], closes: [], cancels: [], radios: [],
      note: null, panel: null, summary: null,
      form: null, accords: []};
    let m;
    const reOpen = /<button\b[^>]*data-dfix-input="[^"]*"[^>]*>/g;
    while ((m = reOpen.exec(html))) {
      const a = __parseAttrs(m[0]);
      const el = tag({kind: "open",
        getAttribute: (k) => (k in a ? a[k] : null),
        disabled: /\bdisabled\b/.test(m[0]), onclick: null});
      el.focus = doFocus(el, "open-focus:" + (a["data-att"] || "?"));
      live.opens.push(el);
    }
    const reClose = /<button\b[^>]*data-dfix-input-close="[^"]*"[^>]*>/g;
    while ((m = reClose.exec(html))) {
      const el = tag({kind: "close",
        getAttribute: (k) => (k === "data-dfix-input-close" ? "1" : null),
        disabled: /\bdisabled\b/.test(m[0]), onclick: null});
      el.focus = doFocus(el, "close-focus");
      live.closes.push(el);
    }
    const reCancel = /<button\b[^>]*data-dfix-cancel="[^"]*"[^>]*>/g;
    while ((m = reCancel.exec(html))) {
      const a = __parseAttrs(m[0]);
      const el = tag({kind: "cancel",
        getAttribute: (k) => (k in a ? a[k] : null),
        disabled: /\bdisabled\b/.test(m[0]), onclick: null});
      el.focus = doFocus(el, "cancel-focus");
      live.cancels.push(el);
    }
    const reAcc = /<details\b[^>]*data-dfix="([^"]*)"[^>]*>/g;
    while ((m = reAcc.exec(html))) {
      if (/\bopen\b/.test(m[0])) {
        const a = __parseAttrs(m[0]);
        live.accords.push(tag({getAttribute: (k) => (k in a ? a[k] : null)}));
      }
    }
    const reRadio = /<input\b[^>]*name="prep-dfix-hyp"[^>]*>/g;
    const group = [];
    while ((m = reRadio.exec(html))) {
      const a = __parseAttrs(m[0]);
      const el = tag({type: "radio", name: "prep-dfix-hyp",
        value: a.value || ""});
      el.__checkedVal = /\bchecked\b/.test(m[0]);
      Object.defineProperty(el, "checked", {
        get() { return el.__checkedVal; },
        set(v) {
          el.__checkedVal = !!v;
          if (v) for (const r of group)
            if (r !== el && r.name === el.name) r.__checkedVal = false;
        },
        configurable: true, enumerable: true});
      el.focus = doFocus(el, "focus:radio:" + (a.value || "?"));
      group.push(el);
      live.radios.push(el);
    }
    const nm = /<textarea\b[^>]*id="prep-dfix-note"[^>]*>([\s\S]*?)<\/textarea>/.exec(html);
    if (nm) {
      const unesc = (s) => String(s).replace(/&quot;/g, '"').replace(/&gt;/g, ">").replace(/&lt;/g, "<").replace(/&amp;/g, "&");
      const el = tag({id: "prep-dfix-note", value: unesc(nm[1]),
        selectionStart: 0, selectionEnd: 0,
        setSelectionRange: (a, b) => { el.selectionStart = a; el.selectionEnd = b; record.push("sel:" + a + ":" + b); }});
      el.focus = doFocus(el, "focus:prep-dfix-note");
      live.note = el;
    }
    const fm = /<details\b[^>]*data-dfix-form[^>]*>/.exec(html);
    if (fm) live.form = tag({get open() { return /\bopen\b/.test(fm[0]); }});
    const pm = /<details\b[^>]*data-dfix-input-panel[^>]*>/.exec(html);
    if (pm) {
      const a = __parseAttrs(pm[0]);
      const el = tag({__open: /\bopen\b/.test(pm[0]),
        get open() { return this.__open; },
        set open(v) { this.__open = !!v; },
        getAttribute: (k) => (k in a ? a[k] : null)});
      live.panel = el;
    }
    if (html.indexOf("data-dfix-input-panel") >= 0) {
      const el = tag({});
      el.focus = doFocus(el, "panel-focus");
      live.summary = el;
    }
    return live;
  }
  function live() { if (!cache) cache = build(); return cache; }
  box.querySelectorAll = (sel) => {
    const L = live();
    if (sel === "[data-dfix-input]") return L.opens;
    if (sel === "[data-dfix-input-close]") return L.closes;
    if (sel === "[data-dfix-cancel]") return L.cancels;
    if (sel === "details[data-dfix][open]") return L.accords;
    if (sel === 'input[name="prep-dfix-hyp"]') return L.radios;
    return [];
  };
  box.querySelector = (sel) => {
    const L = live();
    if (sel === "[data-dfix-input]") return L.opens.length ? L.opens[0] : null;
    if (sel === "[data-dfix-input-close]") return L.closes.length ? L.closes[0] : null;
    if (sel === "[data-dfix-cancel]") return L.cancels.length ? L.cancels[0] : null;
    if (sel === "#prep-dfix-note") return L.note;
    if (sel === "input[name='prep-dfix-hyp']:checked") {
      for (const r of L.radios) if (r.checked) return r;
      return null;
    }
    if (sel === "details[data-dfix-form]") return L.form;
    if (sel === "details[data-dfix-input-panel]") return L.panel;
    if (sel === "[data-dfix-input-panel] summary") return L.summary;
    return null;
  };
  return box;
}
"""


def extract(name, src):
    lines = src.splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^\s*(?:async\s+)?function\s+" + name + r"\s*\(",
                    line):
            start = i
            break
    assert start is not None, name
    depth = 0
    out = []
    for line in lines[start:]:
        out.append(line)
        depth += line.count("{") - line.count("}")
        if out and depth <= 0 and len(out) > 1:
            break
    return "".join(out)


DRIVER = """
const __els = {};
function $(id) { return __els[id] || null; }
function __mkBtn() { return {disabled: false, onclick: null, textContent: ""}; }
function __mkBox() { return {innerHTML: "", textContent: "", dataset: {}}; }
/* Faithful rendered-node lifecycle for ownership tests: replacing
   innerHTML disconnects the old node objects and creates fresh ones,
   so a stale paint's closure can never be re-armed. Only the CURRENT
   render's controls are returned. */
function __mkLiveBox() {
  const box = __mkBox();
  let html = "";
  let cancels = [];
  Object.defineProperty(box, "innerHTML", {
    get: () => html,
    set: (v) => {
      html = String(v);
      cancels.forEach((c) => { c.isConnected = false; });
      cancels = [];
      const re = /data-dfix-cancel="([^"]+)"/g;
      let m;
      while ((m = re.exec(html))) {
        const id = m[1];
        cancels.push({getAttribute: (k) => (k === "data-dfix-cancel" ? id : null),
          disabled: false, onclick: null, isConnected: true});
      }
    },
  });
  box.querySelectorAll = (sel) => {
    if (sel === "[data-dfix-cancel]") return cancels.filter((c) => c.isConnected !== false);
    return [];
  };
  return box;
}
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const toastCalls = []; function toast(t) { toastCalls.push(t); }
const pollCalls = []; function pollJob(id) { pollCalls.push(id); }
let GETS = []; let getCalls = 0;
let __pendingGets = [];
/* Route-aware fixture fetch: the product reads derived-fix first,
   then derived-fix/preflight, then (optionally) the input view.
   Entries marked {__for: "preflight", body} answer ONLY the
   preflight route so a slow preflight fixture can never steal the
   main/input response entries; unmarked entries never leak into
   the preflight route either. Without a marked entry the preflight
   route resolves an honest absent default (no history), exactly
   like a fresh layout — never undefined, never another route's
   payload. Deferred marked entries stay outstanding like any
   other deferred route. */
let PREFLIGHT_DEFAULT = {state: "absent", pfid: null,
  reason: "no verified isolation check", code: "no-preflight",
  history: [], channels: {},
  authority_now: {authorized: false, code: "no-preflight",
    reason: "no verified isolation check"}};
function jget(u) {
  getCalls++;
  const url = String(u);
  if (url.indexOf("derived-fix/preflight") >= 0) {
    for (let i = 0; i < GETS.length; i++) {
      const r = GETS[i];
      if (r && r.__for === "preflight") {
        GETS.splice(i, 1);
        if (r.__deferred === true) {
          let __res;
          const p = new Promise((ok) => { __res = () => ok(r.body !== undefined ? r.body : r.payload); });
          __pendingGets.push({url: u, resolve: __res});
          return p;
        }
        if (r.body instanceof Error) return Promise.reject(r.body);
        return Promise.resolve(r.body);
      }
    }
    return Promise.resolve(PREFLIGHT_DEFAULT);
  }
  for (let i = 0; i < GETS.length; i++) {
    const r = GETS[i];
    if (r && r.__for === "preflight") continue;
    GETS.splice(i, 1);
    if (r instanceof Error) return Promise.reject(r);
    if (r && r.__deferred === true) { let __res; const p = new Promise((ok) => { __res = () => ok(r.payload); }); __pendingGets.push({url: u, resolve: __res}); return p; }
    return Promise.resolve(r);
  }
  return Promise.resolve(undefined);
}
let POSTS = []; let postCalls = [];
function jpost(u, b) { postCalls.push([u, b]); const r = POSTS.shift(); if (r instanceof Error) return Promise.reject(r); return Promise.resolve(r); }
const prepFuDraft = {}, prepFuJobs = {}, prepFuRows = {};
const __timers = [];
/* Timer fixture with bounded delay metadata: entries stay directly
   callable (existing __timers[i]() flows keep working), but each
   scheduled callback also records its delay, so tests execute the
   INTENDED callback — the 5000ms terminal poll, not the 15000ms
   preflight bound — instead of whichever index a new product timer
   happens to occupy. */
function setTimeout(fn, ms) { try { fn.__timerKind = "timeout"; fn.__timerMs = ms; } catch (e) {} __timers.push(fn); return __timers.length; }
function openBatchChanges(a, b) { return []; }
function md(t) { return String(t); }
let window = {confirm: () => CONFIRM};
let CONFIRM = true;
function __setEl(id, el) { __els[id] = el; }
"""


def run_js(tests):
    src = HTML.read_text(encoding="utf-8")
    parts = [DRIVER]
    for name in FUNCTIONS:
        parts.append(extract(name, src))
    parts.append(tests)
    prog = "\n".join(parts)
    with tempfile.NamedTemporaryFile("w", suffix=".js",
                                     delete=False) as tmp:
        tmp.write(";(async () => {\n" + prog + "\n})().catch((e) => { "
                  "console.log('FAIL ' + (e && e.stack || e)); "
                  "process.exit(1); });\n")
        path = tmp.name
    proc = subprocess.run(["node", path], capture_output=True, text=True,
                          timeout=60)
    return proc


BLOCKED_PREP = {
    "op_id": "prep-run-42fd6a6a-nitro-next-eslint-pair-20261019",
    "repo": "react-native-nitro-sound", "lane": "next", "kind": "issue",
    "status": "blocked",
    "branch": "codex/next/issue-856-triage",
    "issue": {"issue": "856", "repo": "react-native-nitro-sound",
              "lane": "next",
              "url": ("https://github.com/hyochan/"
                      "react-native-nitro-sound/issues/856"),
              "branch": "codex/next/issue-856-triage"},
    "result": {"path": "/removed/worktree/proof-not-source",
               "branch": "", "head": "", "fingerprint": "",
               "conflicted_paths": ["example/dist/bundle.c.js",
                                    "example/dist/bundle.c.js.LICENSE.txt",
                                    "example/webpack.config.js"]},
    "error": "cherry-pick conflict. No result: re-derive refused: a, b "
             "besides manifests/locks",
    "gates": [{"key": "source-prepared", "status": "blocked"},
              {"key": "followups", "status": "unrequested"}],
    "followups": [],
    "fastcheck_summary": None,
}


def check(name, tests):
    proc = run_js(tests)
    if proc.returncode != 0 or "FAIL" in (proc.stdout + proc.stderr):
        raise AssertionError(name + "\n" + proc.stdout + "\n" + proc.stderr)
    if "PASS" not in proc.stdout:
        raise AssertionError(name + " produced no PASS\n" + proc.stdout)


class TestRecHelpers(unittest.TestCase):
    def test_conflicted_paths_shape(self):
        check("paths", """
const got = recConflictedPaths({result: {conflicted_paths: ["a/b.js", "/abs", "../up", 5, ""]}});
if (JSON.stringify(got) !== '["a/b.js"]') throw new Error("filter: " + JSON.stringify(got));
if (JSON.stringify(recConflictedPaths({})) !== "[]") throw new Error("empty");
console.log("PASS");
""")

    def test_original_touched_parse(self):
        check("touched", """
const prep = {error: "x. No result: re-derive refused: candidate touches a.js, b.js besides manifests/locks"};
const got = recOriginalTouched(prep);
if (JSON.stringify(got) !== '["a.js","b.js"]') throw new Error(JSON.stringify(got));
if (recOriginalTouched({error: "nothing"}).length !== 0) throw new Error("none");
const junk = {error: "x. No result: re-derive refused: candidate touches a.js, b.js besides manifests/locks y"};
if (recOriginalTouched(junk).length !== 0) throw new Error("trailing junk must reject");
const noprefix = {error: "x. No result: re-derive refused: a.js, b.js besides manifests/locks"};
if (recOriginalTouched(noprefix).length !== 0) throw new Error("missing prefix must reject");
console.log("PASS");
""")

    def test_followup_gating(self):
        check("followup", """
const blocked = %s;
if (recFollowupAllowed(blocked) !== false) throw new Error("blocked must refuse");
const done = Object.assign({}, blocked, {status: "done"});
if (recFollowupAllowed(done) !== true) throw new Error("done must allow");
const plain = {op_id: "x", status: "blocked", result: {path: "", head: ""}};
if (recFollowupAllowed(plain) !== true) throw new Error("non-issue keeps old behavior");
console.log("PASS");
""" % json.dumps(BLOCKED_PREP))

    def test_fastcheck_truth(self):
        check("truth", """
const blocked = %s;
const t = recFastcheckTruth(blocked);
if (!t || t.state !== "not_run") throw new Error("must be not_run");
const withSteps = Object.assign({}, blocked, {fastcheck_summary: {steps: [{}]}});
if (recFastcheckTruth(withSteps) !== null) throw new Error("steps defer");
if (recFastcheckTruth({status: "blocked"}) !== null) throw new Error("non-issue null");
console.log("PASS");
""" % json.dumps(BLOCKED_PREP))


def recovery_payload(**kw):
    base = {"op": "prep-run-42fd6a6a-nitro-next-eslint-pair-20261019",
            "status": "blocked",
            "cause_title": "준비 충돌 — 통합 브랜치를 만들지 못함",
            "conflicted_paths": ["example/dist/bundle.c.js",
                                 "example/dist/bundle.c.js.LICENSE.txt",
                                 "example/webpack.config.js"],
            "original_touched": ["a", "b"],
            "error": "cherry-pick conflict",
            "fastcheck": {"state": "not_run",
                          "reason": "준비 충돌로 미실행 — 검사가 실행되지 않음"},
            "latest": None, "attempts": [], "report": None}
    base.update(kw)
    return base


class TestRecoveryRender(unittest.TestCase):
    def test_blocked_renders_files_and_hides_raw(self):
        check("render", """
const prep = %s;
__setEl("prep-rec", __mkBox());
GETS = [%s];
await paintRecoverySection($("prep-rec"), prep);
const html = $("prep-rec").innerHTML;
["준비 충돌", "실제 충돌 3건", "원본 후보 변경", "준비 충돌로 미실행",
 "원시 Git 실패 기록", "조사 결과 다시 읽기"].forEach(s => {
  if (html.indexOf(s) < 0) throw new Error("missing: " + s);
});
if (html.indexOf("파생 준비는 별도 승인 후에만 가능") < 0) throw new Error("no derived note");
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(recovery_payload())))

    def test_get_failure_readable(self):
        check("getfail", """
const prep = %s;
__setEl("prep-rec", __mkBox());
GETS = [new Error("boom")];
await paintRecoverySection($("prep-rec"), prep);
if ($("prep-rec").innerHTML.indexOf("충돌 조사 읽기 실패") < 0) throw new Error("no error");
console.log("PASS");
""" % json.dumps(BLOCKED_PREP))

    def test_review_posts_confirm_and_polls(self):
        check("review", """
const prep = %s;
const revBtn = __mkBtn(), reBtn = __mkBtn(), box = __mkBox();
__setEl("prep-rec", box);
__setEl("prep-rec-rev-go", revBtn); __setEl("prep-rec-reload", reBtn);
__setEl("prep-rec-msg", {textContent: ""});
GETS = [%s];
POSTS = [{job: {id: "j1"}}];
await paintRecoverySection(box, prep);
if (typeof revBtn.onclick !== "function") throw new Error("no handler");
await revBtn.onclick();
if (postCalls.length !== 1) throw new Error("no post");
if (postCalls[0][0] !== "/api/integration/recovery/review") throw new Error("wrong route");
if (postCalls[0][1].confirm !== true) throw new Error("no confirm");
if (pollCalls.join() !== "j1") throw new Error("no poll");
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(recovery_payload())))

    def test_followup_impossible_shows_guidance(self):
        check("fugate", """
const prep = %s;
const box = __mkBox(); __setEl("prep-fu", box);
paintPrepFollowups(box, prep);
if (box.innerHTML.indexOf("prep-fu-go") >= 0) throw new Error("must not offer followup");
if (box.innerHTML.indexOf("준비된 출처가 없습니다") < 0) throw new Error("no guidance");
console.log("PASS");
""" % json.dumps(BLOCKED_PREP))

    def test_residual_path_is_not_presence(self):
        check("resid", """
const prep = %s;
if (recHasPreparedSource(prep) !== false) throw new Error("residual path is not source");
if (recFollowupAllowed(prep) !== false) throw new Error("followup must stay disabled");
console.log("PASS");
""" % json.dumps(BLOCKED_PREP))

    def test_stale_op_view_is_discarded(self):
        check("race", """
const prep = %s;
const box = __mkBox(); __setEl("prep-rec", box);
let resolveGet;
GETS = [new Promise((res) => { resolveGet = res; })];
const p = paintRecoverySection(box, prep);
box.dataset.recOp = "other-op";
resolveGet(%s);
await p;
if (box.innerHTML !== "") throw new Error("stale view painted");
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(recovery_payload())))

    def test_busy_disables_review_button(self):
        check("busy", """
const prep = %s;
const box = __mkBox(); __setEl("prep-rec", box);
const payload = %s;
payload.latest = {rid: "rr-1", input: "in-1", status: "running"};
GETS = [payload];
await paintRecoverySection(box, prep);
const html = box.innerHTML;
if (html.indexOf("충돌 조사 실행 중") < 0) throw new Error("no busy text");
if (html.indexOf("disabled") < 0) throw new Error("review must be disabled");
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(recovery_payload())))

    def test_sent_evidence_shows_bounded_code_window(self):
        check("codewin", """
const prep = %s;
const box = __mkBox(); __setEl("prep-rec", box);
const payload = %s;
payload.input_files = [{path: "f.txt", ref_kind: "origin", ref: "%s",
  line_count: 3, sent_bytes: 30, text: "line1\\nline2\\nline3"}];
payload.input_omitted = [{path: "dist/bundle.js", reason: "생성 산출물",
  size: 900000}];
GETS = [payload];
await paintRecoverySection(box, prep);
const html = box.innerHTML;
if (html.indexOf("전송된 근거 1건 보기") < 0) throw new Error("no evidence disclosure");
if (html.indexOf("line2") < 0) throw new Error("sent code window missing");
if (html.indexOf("모델에서 제외된 범위 1건 보기") < 0) throw new Error("no omission disclosure");
if (html.indexOf("생성 산출물") < 0) throw new Error("omission reason missing");
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(recovery_payload()),
       "a" * 40))

    def test_review_click_second_get_redraws(self):
        check("redraw", """
const prep = %s;
const revBtn = __mkBtn(), reBtn = __mkBtn(), box = __mkBox();
__setEl("prep-rec", box);
__setEl("prep-rec-rev-go", revBtn); __setEl("prep-rec-reload", reBtn);
__setEl("prep-rec-msg", {textContent: ""});
const donePayload = %s;
donePayload.latest = {rid: "rr-done1", input: "input-x", status: "done", report: "report-x"};
donePayload.report = {summary: "원인 파악됨", cause: "충돌", next_action: "다음"};
/* tick이 보는 종료 상태와 종료 후 다시 읽기가 보는 상태는 모두
   완료된 기록이다: 안정된 서버 상태의 두 번 읽기를 그대로 둔다. */
GETS = [%s, donePayload, donePayload];
POSTS = [{job: {id: "j1"}}];
await paintRecoverySection(box, prep);
const before = getCalls;
await revBtn.onclick();
if (postCalls.length !== 1) throw new Error("no post");
if (pollCalls.join() !== "j1") throw new Error("no poll");
if (__timers.length !== 1) throw new Error("no tick scheduled");
await __timers[0]();
if (getCalls !== before + 2) throw new Error("tick + terminal repaint must re-read twice, got " + (getCalls - before));
if (box.innerHTML.indexOf("원인 파악됨") < 0) throw new Error("terminal report not painted");
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(recovery_payload()),
       json.dumps(recovery_payload())))

    def test_review_reuse_repaints_without_job(self):
        check("reuse", """
const prep = %s;
const revBtn = __mkBtn(), reBtn = __mkBtn(), box = __mkBox();
__setEl("prep-rec", box);
__setEl("prep-rec-rev-go", revBtn); __setEl("prep-rec-reload", reBtn);
__setEl("prep-rec-msg", {textContent: ""});
const donePayload = %s;
donePayload.latest = {rid: "rr-done1", input: "input-x", status: "done", report: "report-x"};
donePayload.current = true;
donePayload.report = {summary: "원인 파악됨", cause: "충돌", next_action: "다음"};
/* 재사용 후 다시 읽기는 완료된 기록을 본다: 빈 초기 페이로드가
   아니라 확정된 보고서를 그린다. */
GETS = [%s, donePayload];
POSTS = [{reused: true, attached: false, reason: "동일 입력 재사용"}];
await paintRecoverySection(box, prep);
const before = getCalls;
await revBtn.onclick();
if (postCalls.length !== 1) throw new Error("no post");
if (pollCalls.length !== 0) throw new Error("reuse must not poll a job");
if (__timers.length !== 0) throw new Error("reuse must not schedule a tick");
if (getCalls !== before + 1) throw new Error("reuse must re-read once");
if (box.innerHTML.indexOf("원인 파악됨") < 0) throw new Error("reused report not painted");
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(recovery_payload()),
       json.dumps(recovery_payload())))

    def test_detached_box_paint_is_noop(self):
        check("detached", """
const prep = %s;
const box = __mkBox(); __setEl("prep-rec", box);
const payload = %s;
GETS = [payload];
await paintRecoverySection(box, prep);
const rendered = box.innerHTML;
box.isConnected = false;
GETS = [%s];
await paintRecoverySection(box, prep);
if (box.innerHTML !== rendered) throw new Error("detached paint must not touch DOM");
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(recovery_payload()),
       json.dumps(recovery_payload())))

    def test_review_click_stale_tick_dropped(self):
        check("staletick", """
const prep = %s;
const revBtn = __mkBtn(), reBtn = __mkBtn(), box = __mkBox();
__setEl("prep-rec", box);
__setEl("prep-rec-rev-go", revBtn); __setEl("prep-rec-reload", reBtn);
__setEl("prep-rec-msg", {textContent: ""});
GETS = [%s];
POSTS = [{job: {id: "j1"}}];
await paintRecoverySection(box, prep);
await revBtn.onclick();
const calls = getCalls;
// A newer paint on the same box invalidates the pending tick.
box.dataset.recSeq = String(parseInt(box.dataset.recSeq || "0", 10) + 5);
await __timers[0]();
if (getCalls !== calls) throw new Error("stale tick must not re-read");
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(recovery_payload())))

    def test_pending_gate_absent_receipt_not_run(self):
        check("gatefail", """
const prep = %s;
const box = __mkBox(); __setEl("prep-rec", box);
const payload = %s;
payload.fastcheck = {state: "unknown", reason: "게이트 없음"};
payload.gates = [{key: "source-prepared", status: "blocked"}];
GETS = [payload];
await paintRecoverySection(box, prep);
const html = box.innerHTML;
if (html.indexOf("준비 충돌로 미실행") >= 0) throw new Error("gate fail must not claim not_run");
if (html.indexOf("검사 기록 문제") < 0) throw new Error("must surface gate truth");
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(recovery_payload())))

    def test_full_identifiers_reachable(self):
        check("idents", """
const prep = %s;
const box = __mkBox(); __setEl("prep-rec", box);
const payload = %s;
payload.candidate = {head: "%s", fingerprint: "%s"};
payload.base = {head: "%s", fingerprint: "%s"};
payload.latest = {rid: "rr-abc", input: "in-def", status: "done"};
GETS = [payload];
await paintRecoverySection(box, prep);
const html = box.innerHTML;
["전체 식별자 보기", "%s", "%s"].forEach(s => {
  if (html.indexOf(s) < 0) throw new Error("missing identifier: " + s);
});
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(recovery_payload()),
       "a" * 40, "c" * 64, "b" * 40, "d" * 64, "a" * 40, "d" * 64))

    def test_binding_error_history_display(self):
        check("bindhist", """
const prep = %s;
__setEl("prep-rec", __mkBox());
const payload = %s;
payload.current = false;
payload.binding_error = {code: "stale", message: "base changed"};
payload.latest = {rid: "rr-old", input: "in-old", status: "stale"};
payload.attempts = [{rid: "rr-old", status: "error", input: "in-old"}];
payload.report = {summary: "old stored summary"};
GETS = [payload];
await paintRecoverySection($("prep-rec"), prep);
const html = $("prep-rec").innerHTML;
["현재 원본 바인딩 실패", "stale", "보존된 과거 기록",
 "조사 완료(저장됨·현재 아님)", "old stored summary",
 "rr-old"].forEach(s => {
  if (html.indexOf(s) < 0) throw new Error("missing history: " + s);
});
if (html.indexOf("조사 완료(현재 유효)") >= 0) throw new Error("must not claim current");
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(recovery_payload())))

    def test_attempt_total_truncation_note(self):
        # attempt_total counts all names; only 20 are projected.
        check("atttotal", """
const prep = %s;
__setEl("prep-rec", __mkBox());
const payload = %s;
payload.attempt_total = 25;
payload.attempts = [{rid: "rr-1", status: "done", input: "in-1"},
                    {rid: "rr-2", status: "error", input: "in-2"}];
GETS = [payload];
await paintRecoverySection($("prep-rec"), prep);
const html = $("prep-rec").innerHTML;
["조사 시도 25건 중 최근 2건 표시", "조사 시도 25건 보기"].forEach(s => {
  if (html.indexOf(s) < 0) throw new Error("missing total: " + s);
});
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(recovery_payload())))

    def test_stored_labels_rendered_not_live(self):
        # A3: the historical view must show the validated stored
        # candidate/base heads, never the live parent record.
        check("storedlabels", """
const prep = %s;
__setEl("prep-rec", __mkBox());
const payload = %s;
payload.candidate = {head: "%s", fingerprint: "%s"};
payload.base = {head: "%s", fingerprint: "%s"};
GETS = [payload];
await paintRecoverySection($("prep-rec"), prep);
const html = $("prep-rec").innerHTML;
["%s", "%s", "%s", "원본"].forEach(s => {
  if (html.indexOf(s) < 0) throw new Error("missing stored: " + s);
});
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(recovery_payload()),
       "c" * 40, "e" * 64, "f" * 40, "0" * 64,
       "c" * 40, "f" * 40, "e" * 64))

    def test_omitted_absent_reason(self):
        # Context case: an omitted path proven absent at the pinned
        # commit must say so explicitly.
        check("omitabsent", """
const prep = %s;
__setEl("prep-rec", __mkBox());
const payload = %s;
payload.input_omitted = [{path: "gone.txt", reason: "cap",
                          absent: true}];
GETS = [payload];
await paintRecoverySection($("prep-rec"), prep);
const html = $("prep-rec").innerHTML;
["gone.txt", "고정 커밋에 없음(확인됨)"].forEach(s => {
  if (html.indexOf(s) < 0) throw new Error("missing omitted: " + s);
});
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(recovery_payload())))


def full_model_report():
    return {"status": "needs_changes",
            "summary": "후보와 베이스가 같은 위치를 다르게 고침",
            "cause": "webpack 규칙 충돌",
            "conflict_files": ["example/webpack.config.js"],
            "citations": [], "intended_changes": [],
            "proposed_paths": ["example/webpack.config.js",
                               "src/__tests__/webpack-web.test.ts"],
            "build_contract": None,
            "questions": ["베이스 webpack 수정을 유지한 채 후보 테스트만 재적용할 수 있는가? Q1-END-7f3a",
                          "dist 생성물을 직접 병합하지 않고 소스 확정 후 재생성하는 절차로 합의할 수 있는가? 추가 확인을 위한 매우 긴 설명 문장이다. " + "가" * 220 + "\n두 번째 줄 확인 Q2-END-9b1e",
                          "index.html이 가리키는 cc0231 번들의 동일성을 어떤 증거로 확인할 것인가? Q3-END-4c2d"],
            "uncertainties": ["타입 관련 변경이 실제 소스 변경인지 내부 타입 이슈 용어의 추정 표현인지 불확실함 U1-END-a5f0"],
            "limits": ["모델 입력에서 빠진 dist 번들 바이트는 판단하지 않음 L1-END-1a2b",
                       "전송되지 않은 ref·줄은 인용하지 않음 L2-END-3c4d"],
            "next_action": "베이스 수정을 유지하고 후보 테스트만 수동 재적용할지 결정"}


class TestRecoveryReportFields(unittest.TestCase):
    def test_current_done_needs_changes_full_fields(self):
        check("repofull", """
const prep = %s;
const box = __mkBox(); __setEl("prep-rec", box);
const payload = %s;
payload.current = true;
payload.latest = {rid: "rr-new", input: "in-new", status: "done", report: "rep-new"};
payload.report = %s;
GETS = [payload];
await paintRecoverySection(box, prep);
const html = box.innerHTML;
["조사 완료(현재 유효)", "모델 판정 — 변경 필요 (needs_changes)",
 "모델 보고서 — 제안이며 확정 코드·승인이 아님",
 "모델 질문 3건", "Q1-END-7f3a",
 "Q2-END-9b1e", "두 번째 줄 확인", "Q3-END-4c2d",
 "모델 불확실성 1건", "타입 관련 변경", "U1-END-a5f0",
 "추정이며 확정 사실 아님", "모델 제한 2건",
 "L1-END-1a2b", "L2-END-3c4d",
 "모델 제안 경로 2건", "제안이며 승인 아님",
 "example/webpack.config.js", "src/__tests__/webpack-web.test.ts",
 "빌드 계약 — 미정립(builder/command/outputs 없음)"].forEach(s => {
  if (html.indexOf(s) < 0) throw new Error("missing model field: " + s);
});
if (html.indexOf("파생 준비 검토 가능") >= 0) throw new Error("must not imply ready");
if (html.indexOf("<br>") < 0) throw new Error("line break must be preserved");
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(recovery_payload()),
       json.dumps(full_model_report())))

    def test_ready_verdict_never_auto_approval(self):
        check("repoready", """
const prep = %s;
const box = __mkBox(); __setEl("prep-rec", box);
const payload = %s;
payload.current = true;
payload.latest = {rid: "rr-r", input: "in-r", status: "done", report: "rep-r"};
const rep = %s;
rep.status = "ready_for_derived";
payload.report = rep;
GETS = [payload];
await paintRecoverySection(box, prep);
const html = box.innerHTML;
["조사 완료(현재 유효)", "모델 판정 — 파생 준비 검토 가능 (ready_for_derived)",
 "자동 승인 아님"].forEach(s => {
  if (html.indexOf(s) < 0) throw new Error("missing ready verdict: " + s);
});
if (html.indexOf("모델 판정 — 변경 필요") >= 0) throw new Error("verdict must match status");
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(recovery_payload()),
       json.dumps(full_model_report())))

    def test_undecided_and_unknown_verdicts_distinct(self):
        check("repoundec", """
const prep = %s;
const box = __mkBox(); __setEl("prep-rec", box);
const payload = %s;
payload.current = true;
payload.latest = {rid: "rr-u", input: "in-u", status: "done", report: "rep-u"};
const rep = %s;
rep.status = "undecided";
payload.report = rep;
GETS = [payload];
await paintRecoverySection(box, prep);
if (box.innerHTML.indexOf("모델 판정 — 판단 보류 (undecided)") < 0) throw new Error("no undecided");
const box2 = __mkBox(); __setEl("prep-rec", box2);
const payload2 = %s;
payload2.current = true;
payload2.latest = {rid: "rr-x", input: "in-x", status: "done", report: "rep-x"};
const rep2 = %s;
delete rep2.status;
payload2.report = rep2;
GETS = [payload2];
await paintRecoverySection(box2, prep);
const html2 = box2.innerHTML;
if (html2.indexOf("모델 판정 — 미기록 (unknown)") < 0) throw new Error("no unknown");
["변경 필요", "파생 준비 검토 가능", "판단 보류"].forEach(s => {
  if (html2.indexOf(s) >= 0) throw new Error("unknown must not claim verdict: " + s);
});
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(recovery_payload()),
       json.dumps(full_model_report()), json.dumps(recovery_payload()),
       json.dumps(full_model_report())))

    def test_stale_fields_stay_noncurrent(self):
        check("repostale", """
const prep = %s;
const box = __mkBox(); __setEl("prep-rec", box);
const payload = %s;
payload.current = false;
payload.latest = {rid: "rr-old", input: "in-old", status: "stale", report: "rep-old"};
payload.report = %s;
GETS = [payload];
await paintRecoverySection(box, prep);
const html = box.innerHTML;
["조사 완료(저장됨·현재 아님)", "모델 판정 — 변경 필요 (needs_changes)",
 "모델 질문 3건", "타입 관련 변경"].forEach(s => {
  if (html.indexOf(s) < 0) throw new Error("missing stale model field: " + s);
});
if (html.indexOf("조사 완료(현재 유효)") >= 0) throw new Error("stale must not claim current");
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(recovery_payload()),
       json.dumps(full_model_report())))

    def test_build_contract_object_and_empty_vs_missing(self):
        check("repocontract", """
const prep = %s;
const box = __mkBox(); __setEl("prep-rec", box);
const payload = %s;
payload.current = true;
payload.latest = {rid: "rr-c", input: "in-c", status: "done", report: "rep-c"};
const rep = %s;
rep.build_contract = {builder: "npm", command: "npm run build",
                      outputs: ["dist/a.js", "dist/b.js"]};
payload.report = rep;
GETS = [payload];
await paintRecoverySection(box, prep);
["빌드 계약", "npm", "npm run build", "dist/a.js",
 "dist/b.js"].forEach(s => {
  if (box.innerHTML.indexOf(s) < 0) throw new Error("missing contract: " + s);
});
const box2 = __mkBox(); __setEl("prep-rec", box2);
const payload2 = %s;
payload2.current = true;
payload2.latest = {rid: "rr-e", input: "in-e", status: "done", report: "rep-e"};
const rep2 = %s;
rep2.questions = []; rep2.uncertainties = []; rep2.limits = [];
rep2.proposed_paths = [];
rep2.build_contract = {builder: "npm", command: "npm run build", outputs: []};
payload2.report = rep2;
GETS = [payload2];
await paintRecoverySection(box2, prep);
["모델 질문 0건", "모델 불확실성 0건", "모델 제한 0건",
 "모델 제안 경로 0건", "산출물 0건"].forEach(s => {
  if (box2.innerHTML.indexOf(s) < 0) throw new Error("missing empty: " + s);
});
const box3 = __mkBox(); __setEl("prep-rec", box3);
const payload3 = %s;
payload3.current = true;
payload3.latest = {rid: "rr-m", input: "in-m", status: "done", report: "rep-m"};
const rep3 = %s;
delete rep3.questions; delete rep3.build_contract;
payload3.report = rep3;
GETS = [payload3];
await paintRecoverySection(box3, prep);
["모델 질문 — 미기록", "빌드 계약 — 미기록"].forEach(s => {
  if (box3.innerHTML.indexOf(s) < 0) throw new Error("missing absent: " + s);
});
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(recovery_payload()),
       json.dumps(full_model_report()), json.dumps(recovery_payload()),
       json.dumps(full_model_report()), json.dumps(recovery_payload()),
       json.dumps(full_model_report())))

    def test_model_markup_escaped_not_executable(self):
        check("repoesc", """
const prep = %s;
const box = __mkBox(); __setEl("prep-rec", box);
const payload = %s;
payload.current = true;
payload.latest = {rid: "rr-s", input: "in-s", status: "done", report: "rep-s"};
const rep = %s;
rep.questions = ["<script>qq-a1</script>"];
rep.uncertainties = ["불확실 <uu-inject-b2> 확인"];
rep.limits = ["제한 <ll-inject-c3> 확인"];
rep.proposed_paths = ["<pp-inject-d4>/x.js"];
rep.cause = '<img src=x onerror="alert(2)">';
rep.build_contract = {builder: "<b>esc</b>",
                      command: "<cc-inject-e5>run</cc-inject-e5>",
                      outputs: ["<oo-inject-f6>out.js</oo-inject-f6>"]};
payload.report = rep;
GETS = [payload];
await paintRecoverySection(box, prep);
const html = box.innerHTML;
["&lt;script&gt;qq-a1&lt;/script&gt;", "&lt;uu-inject-b2&gt;",
 "&lt;ll-inject-c3&gt;", "&lt;pp-inject-d4&gt;", "&lt;img",
 "&lt;b&gt;esc&lt;/b&gt;", "&lt;cc-inject-e5&gt;",
 "&lt;oo-inject-f6&gt;"].forEach(s => {
  if (html.indexOf(s) < 0) throw new Error("not escaped: " + s);
});
["<script>qq-a1</script>", "<uu-inject-b2>", "<ll-inject-c3>",
 "<pp-inject-d4>", "<b>esc</b>", "<cc-inject-e5>", "<oo-inject-f6>",
 '<img src=x'].forEach(s => {
  if (html.indexOf(s) >= 0) throw new Error("raw markup leaked: " + s);
});
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(recovery_payload()),
       json.dumps(full_model_report())))


def ctx_payload(**kw):
    p = recovery_payload()
    p["current"] = True
    p["latest"] = {"rid": "rr-ctx", "input": "input-ctx",
                   "status": "done", "report": "report-ctx",
                   "fingerprint": "f" * 64}
    p["report"] = full_model_report()
    p["context_allowed"] = True
    p["origin_shas"] = {"input": "a" * 64, "report": "b" * 64}
    p["contexts"] = []
    p.update(kw)
    return p


class TestRecoveryContextUI(unittest.TestCase):
    def test_form_gated_by_context_allowed(self):
        check("ctxgate", """
const prep = %s;
const box = __mkBox(); __setEl("prep-rec", box);
GETS = [%s];
await paintRecoverySection(box, prep);
if (box.innerHTML.indexOf("추가 맥락 제공") < 0) throw new Error("no form");
if (box.innerHTML.indexOf('id="prep-rec-ctx-text"') < 0) throw new Error("no textarea");
if (box.innerHTML.indexOf("min-height:44px") < 0) throw new Error("no 44px targets");
const box2 = __mkBox(); __setEl("prep-rec", box2);
const denied = %s;
denied.context_allowed = false;
GETS = [denied];
await paintRecoverySection(box2, prep);
if (box2.innerHTML.indexOf("추가 맥락 제공") >= 0) throw new Error("denied must hide form");
const box3 = __mkBox(); __setEl("prep-rec", box3);
const unknown = %s;
delete unknown.context_allowed;
GETS = [unknown];
await paintRecoverySection(box3, prep);
if (box3.innerHTML.indexOf("추가 맥락 제공") >= 0) throw new Error("unknown must hide form");
const box4 = __mkBox(); __setEl("prep-rec", box4);
const busy = %s;
busy.latest = Object.assign({}, busy.latest, {status: "running"});
GETS = [busy];
await paintRecoverySection(box4, prep);
if (box4.innerHTML.indexOf("추가 맥락 제공") >= 0) throw new Error("busy must hide form");
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(ctx_payload()),
       json.dumps(ctx_payload()), json.dumps(ctx_payload()),
       json.dumps(ctx_payload())))

    def test_form_shows_full_questions_and_honest_counts(self):
        check("ctxfull", """
const prep = %s;
const box = __mkBox(); __setEl("prep-rec", box);
GETS = [%s];
await paintRecoverySection(box, prep);
const html = box.innerHTML;
["모델 질문 3건", "Q1-END-7f3a", "Q2-END-9b1e", "두 번째 줄 확인",
 "Q3-END-4c2d", "저장된 맥락 기록 0건",
 "소유자 정책 질문은 맥락 저장 후에도 미확인 상태로 남습니다",
 "Enter는 줄바꿈"].forEach(s => {
  if (html.indexOf(s) < 0) throw new Error("missing form field: " + s);
});
if (html.indexOf("미답변") >= 0) throw new Error("no inferred unanswered count");
if (html.indexOf("(보충됨)") >= 0) throw new Error("no inferred answered marks");
if (html.indexOf("<br>") < 0) throw new Error("line break must be preserved");
if (html.indexOf("<form") >= 0) throw new Error("no form element: Enter must stay newline");
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(ctx_payload())))

    def test_stored_context_text_visible_and_escaped(self):
        check("ctxkept", """
const prep = %s;
const box = __mkBox(); __setEl("prep-rec", box);
const payload = %s;
payload.contexts = [{id: "context-0123456789abcdef", actor: "operator",
  question_refs: [0], text: "<b>note-x1</b> 메모 본문",
  text_len: 14, created: "2026-10-29T00:00:00Z",
  origin: {input: "input-ctx", report: "report-ctx"}}];
GETS = [payload];
await paintRecoverySection(box, prep);
const html = box.innerHTML;
["저장된 맥락 기록 1건 보기 (사용 여부 구분)", "context-0123456789abcdef",
 "운용자(operator)", "&lt;b&gt;note-x1&lt;/b&gt; 메모 본문",
 "report-ctx", "저장 기록 (현재 입력 사용 여부 미확인)"].forEach(s => {
  if (html.indexOf(s) < 0) throw new Error("missing kept context: " + s);
});
if (html.indexOf("<b>note-x1</b>") >= 0) throw new Error("raw markup leaked");
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(ctx_payload())))

    def test_stale_draft_retained_readonly(self):
        check("ctxstale", """
const prep = %s;
const box = __mkBox(); __setEl("prep-rec", box);
box._ctxDraft = {input: "input-old", report: "report-old",
  inputSha: new Array(65).join("0"), reportSha: new Array(65).join("1"),
  text: "낡은 메모 <b>raw-x9</b>", refs: ["0"], focused: false};
const payload = %s;
GETS = [payload, payload];
await paintRecoverySection(box, prep);
const html = box.innerHTML;
["이전 초안 보기", "낡은 메모", "&lt;b&gt;raw-x9&lt;/b&gt;",
 "제출되지 않음", "읽기 전용", "readonly"].forEach(s => {
  if (html.indexOf(s) < 0) throw new Error("missing stale draft: " + s);
});
if (html.indexOf("<b>raw-x9</b>") >= 0) throw new Error("raw markup leaked");
if (box.innerHTML.indexOf('id="prep-rec-ctx-text"') < 0) throw new Error("fresh form missing");
if (!box._ctxStaleKept || box._ctxStaleKept.text !== "낡은 메모 <b>raw-x9</b>") throw new Error("stale not retained");
if (box._ctxDraft !== null) throw new Error("active draft must clear on chain change");
await paintRecoverySection(box, prep);
if (box.innerHTML.indexOf("이전 초안 보기") < 0) throw new Error("stale must survive repaint");
if (box.innerHTML.indexOf("낡은 메모") < 0) throw new Error("stale text must survive repaint");
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(ctx_payload())))

    def test_submit_unknown_response_keeps_draft(self):
        check("ctxunknown", """
const prep = %s;
const box = __mkBox(); __setEl("prep-rec", box);
__setEl("prep-rec-ctx-go", {disabled: false, onclick: null});
__setEl("prep-rec-ctx-text", {value: "운용자 메모 본문"});
__setEl("prep-rec-msg", {textContent: ""});
box._ctxDraft = {input: "input-ctx", report: "report-ctx",
  inputSha: new Array(65).join("a"), reportSha: new Array(65).join("b"),
  text: "초안유지", refs: [], focused: false};
GETS = [%s];
await paintRecoverySection(box, prep);
POSTS = [{}];
await __els["prep-rec-ctx-go"].onclick();
const msg = __els["prep-rec-msg"].textContent || "";
if (msg.indexOf("확인할 수 없습니다") < 0) throw new Error("unknown must say so: " + msg);
if (!box._ctxDraft || box._ctxDraft.text !== "초안유지") throw new Error("draft must be kept");
if (__els["prep-rec-ctx-go"].disabled !== false) throw new Error("button must re-enable");
if (pollCalls.length !== 0) throw new Error("no poll on unknown");
const body = postCalls[0][1];
if (body.context.expected.input_sha256 !== new Array(65).join("a")) throw new Error("expected sha missing");
if (body.context.text !== "운용자 메모 본문") throw new Error("text missing");
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(ctx_payload())))

    def test_submit_started_without_job_is_unknown(self):
        check("ctxnojob", """
const prep = %s;
const box = __mkBox(); __setEl("prep-rec", box);
__setEl("prep-rec-ctx-go", {disabled: false, onclick: null});
__setEl("prep-rec-ctx-text", {value: "메모"});
__setEl("prep-rec-msg", {textContent: ""});
GETS = [%s];
await paintRecoverySection(box, prep);
POSTS = [{started: true, reused: false, rid: "rr-9", op: "x"}];
await __els["prep-rec-ctx-go"].onclick();
const msg = __els["prep-rec-msg"].textContent || "";
if (msg.indexOf("확인할 수 없습니다") < 0) throw new Error("started-without-job must be unknown: " + msg);
if (pollCalls.length !== 0) throw new Error("no poll without a valid job");
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(ctx_payload())))

    def test_submit_reused_vs_attached_truth(self):
        check("ctxreuse", """
const prep = %s;
const OP = prep.op_id;
const box = __mkBox(); __setEl("prep-rec", box);
__setEl("prep-rec-ctx-go", {disabled: false, onclick: null});
__setEl("prep-rec-ctx-text", {value: "메모"});
__setEl("prep-rec-msg", {textContent: ""});
const payload = %s;
GETS = [payload, payload];
await paintRecoverySection(box, prep);
POSTS = [{started: false, reused: true, attached: false, rid: "rr-7", op: OP,
          reason: "identical review already recorded; no new run",
          context: {id: "context-0123456789abcdef", reused: true}}];
await __els["prep-rec-ctx-go"].onclick();
if ((__els["prep-rec-msg"].textContent || "").indexOf("재사용합니다") < 0) throw new Error("no reuse message");
if (box._ctxDraft !== null) throw new Error("draft must clear on confirmed reuse");
const box2 = __mkBox(); __setEl("prep-rec", box2);
__setEl("prep-rec-ctx-go", {disabled: false, onclick: null});
__setEl("prep-rec-ctx-text", {value: "메모2"});
__setEl("prep-rec-msg", {textContent: ""});
const payload2 = %s;
GETS = [payload2, payload2];
await paintRecoverySection(box2, prep);
POSTS = [{started: false, reused: true, attached: true, rid: "rr-8", op: OP,
          job_id: "job-live-8",
          context: {id: "context-0123456789abcdef", reused: false}}];
await __els["prep-rec-ctx-go"].onclick();
if ((__els["prep-rec-msg"].textContent || "").indexOf("진행 중인 동일 조사에 연결") < 0) throw new Error("no attach message");
if (box2._ctxDraft !== null) throw new Error("draft must clear on confirmed attach");
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(ctx_payload()),
       json.dumps(ctx_payload())))

    def test_submit_started_positive_polls_and_clears(self):
        check("ctxstarted", """
const prep = %s;
const OP = prep.op_id;
const box = __mkBox(); __setEl("prep-rec", box);
__setEl("prep-rec-ctx-go", {disabled: false, onclick: null});
__setEl("prep-rec-ctx-text", {value: "메모"});
__setEl("prep-rec-msg", {textContent: ""});
GETS = [%s];
await paintRecoverySection(box, prep);
POSTS = [{started: true, reused: false, attached: false, op: OP, rid: "rr-10",
          job: {id: "job-3"},
          context: {id: "context-aaaabbbbccccdddd", reused: false}}];
await __els["prep-rec-ctx-go"].onclick();
if ((__els["prep-rec-msg"].textContent || "").indexOf("조사 실행 중") < 0) throw new Error("no running message");
if (JSON.stringify(pollCalls) !== '["job-3"]') throw new Error("must poll the real job: " + JSON.stringify(pollCalls));
if (box._ctxDraft !== null) throw new Error("draft must clear on confirmed start");
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(ctx_payload())))

    def test_submit_malformed_or_foreign_never_claims(self):
        check("ctxbadresp", """
const prep = %s;
const OP = prep.op_id;
const box = __mkBox(); __setEl("prep-rec", box);
__setEl("prep-rec-ctx-go", {disabled: false, onclick: null});
__setEl("prep-rec-ctx-text", {value: "메모"});
__setEl("prep-rec-msg", {textContent: ""});
GETS = [%s];
await paintRecoverySection(box, prep);
const good = {started: true, reused: false, attached: false, op: OP, rid: "rr-9",
  job: {id: "job-9"}, context: {id: "context-9999000011112222", reused: false}};
const cases = [
  Object.assign({}, good, {job: {id: 17}}),
  Object.assign({}, good, {job: {id: {}}}),
  Object.assign({}, good, {reused: true}),
  Object.assign({}, good, {op: "prep-foreign-0000"}),
  Object.assign({}, good, {rid: ""}),
  {started: true, reused: false, attached: false, op: OP, rid: "rr-9",
   job: {id: "job-9"}},
  Object.assign({}, good, {attached: true, job_id: null}),
  Object.assign({}, good, {started: "true"}),
];
for (const c of cases) {
  POSTS = [c];
  await __els["prep-rec-ctx-go"].onclick();
  const msg = __els["prep-rec-msg"].textContent || "";
  if (msg.indexOf("확인할 수 없습니다") < 0) throw new Error("must stay unknown: " + JSON.stringify(c));
  if (pollCalls.length !== 0) throw new Error("no poll on bad response");
}
if (__els["prep-rec-ctx-go"].disabled !== false) throw new Error("button must stay enabled");
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(ctx_payload())))

    def test_malformed_revision_hashes_hide_form_and_send_nothing(self):
        check("ctxbadsha", """
const prep = %s;
const box = __mkBox(); __setEl("prep-rec", box);
const base = %s;
const goods = {input: new Array(65).join("a"),
  report: new Array(65).join("b")};
const bads = ["z", new Array(64).join("a"), new Array(65).join("A"),
  " " + new Array(64).join("b"), new Array(64).join("c") + " ", 17, null];
const payloads = [];
bads.forEach(v => {
  const p1 = JSON.parse(JSON.stringify(base));
  p1.origin_shas = {input: v, report: goods.report};
  payloads.push(p1);
  const p2 = JSON.parse(JSON.stringify(base));
  p2.origin_shas = {input: goods.input, report: v};
  payloads.push(p2);
});
GETS = payloads.slice();
for (const p of payloads) {
  await paintRecoverySection(box, prep);
  if (box.innerHTML.indexOf("추가 맥락 제공") >= 0) throw new Error("form must hide for malformed hashes");
  if (box.innerHTML.indexOf('id="prep-rec-ctx-text"') >= 0) throw new Error("textarea must hide for malformed hashes");
}
const box2 = __mkBox(); __setEl("prep-rec", box2);
__setEl("prep-rec-ctx-go", {disabled: false, onclick: null});
__setEl("prep-rec-ctx-text", {value: "메모"});
__setEl("prep-rec-msg", {textContent: ""});
box2._ctxDraft = {input: "input-ctx", report: "report-ctx",
  inputSha: new Array(65).join("a"), reportSha: "z",
  text: "보존메모", refs: [], focused: false};
const badone = JSON.parse(JSON.stringify(base));
badone.origin_shas = {input: goods.input, report: "z"};
GETS = [badone];
await paintRecoverySection(box2, prep);
if (!box2._ctxStaleKept || box2._ctxStaleKept.text !== "보존메모") throw new Error("stale draft must be retained");
if (box2._ctxDraft !== null) throw new Error("active draft must clear on chain change");
await __els["prep-rec-ctx-go"].onclick();
const msg = __els["prep-rec-msg"].textContent || "";
if (msg.indexOf("원본 바이트 정보") < 0) throw new Error("must refuse without valid hashes: " + msg);
if (postCalls.length !== 0) throw new Error("must not POST malformed revision");
if (!box2._ctxStaleKept || box2._ctxStaleKept.text !== "보존메모") throw new Error("retained draft must survive refused POST");
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(ctx_payload())))

    def test_evidence_visible_without_submission_affordance(self):
        check("ctxevidence", """
const prep = %s;
const box = __mkBox(); __setEl("prep-rec", box);
function evPayload() {
  const p = %s;
  p.latest = {rid: "rr-ev-B", input: "input-BBBB3333",
    status: "done", report: "report-BBBB3333",
    fingerprint: new Array(65).join("f")};
  p.current = true;
  p.context_allowed = true;
  p.origin_shas = {input: new Array(65).join("c"),
    report: new Array(65).join("d")};
  p.current_context = {id: "context-eeee111122223333", actor: "operator",
    question_refs: [0], text: "증거 메모 EVIDENCE-r4",
    text_len: 16, created: "2026-10-29T00:00:00Z",
    origin: {input: "input-AAAA1111", report: "report-AAAA1111",
      fingerprint: new Array(65).join("e"),
      input_sha256: new Array(65).join("a"),
      report_sha256: new Array(65).join("b"),
      questions: [{index: 0, text: "원문 질문 EVIDENCE-q9"}]},
    consumed_by: new Array(65).join("f")};
  p.contexts = [
    {id: "context-eeee111122223333", actor: "operator",
     question_refs: [0], text: "증거 메모 EVIDENCE-r4",
     text_len: 16, created: "2026-10-29T00:00:00Z",
     origin: {input: "input-AAAA1111", report: "report-AAAA1111"}},
    {id: "context-ffff444455556666", actor: "operator",
     question_refs: [], text: "다른 기록", text_len: 5,
     created: "2026-10-29T00:00:00Z",
     origin: {input: "input-AAAA1111", report: "report-AAAA1111"}}];
  return p;
}
const busy = evPayload();
busy.latest = Object.assign({}, busy.latest, {status: "running"});
const stale = evPayload();
stale.current = false;
const denied = evPayload();
denied.context_allowed = false;
const nohash = evPayload();
delete nohash.origin_shas;
GETS = [busy, stale, denied, nohash];
const wants = ["현재 검토 입력에 사용된 맥락", "EVIDENCE-r4",
 "원문 질문 EVIDENCE-q9", "input-AAAA1111", "report-AAAA1111",
 "현재 입력에 사용됨", "저장 기록 (현재 입력 사용 여부 미확인)"];
await paintRecoverySection(box, prep);
let html = box.innerHTML;
if (html.indexOf("추가 맥락 제공") >= 0) throw new Error("busy must hide form");
wants.forEach(s => {
  if (html.indexOf(s) < 0) throw new Error("busy must keep evidence: " + s);
});
await paintRecoverySection(box, prep);
html = box.innerHTML;
if (html.indexOf("추가 맥락 제공") >= 0) throw new Error("stale must hide form");
if (html.indexOf("현재 유효") >= 0) throw new Error("stale must not claim current");
wants.forEach(s => {
  if (html.indexOf(s) < 0) throw new Error("stale must keep evidence: " + s);
});
await paintRecoverySection(box, prep);
html = box.innerHTML;
if (html.indexOf("추가 맥락 제공") >= 0) throw new Error("denied must hide form");
wants.forEach(s => {
  if (html.indexOf(s) < 0) throw new Error("denied must keep evidence: " + s);
});
await paintRecoverySection(box, prep);
html = box.innerHTML;
if (html.indexOf("추가 맥락 제공") >= 0) throw new Error("nohash must hide form");
wants.forEach(s => {
  if (html.indexOf(s) < 0) throw new Error("nohash must keep evidence: " + s);
});
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(ctx_payload())))

    def test_submit_without_revision_hashes_sends_nothing(self):
        check("ctxnoshare", """
const prep = %s;
const box = __mkBox(); __setEl("prep-rec", box);
__setEl("prep-rec-ctx-go", {disabled: false, onclick: null});
__setEl("prep-rec-ctx-text", {value: "메모"});
__setEl("prep-rec-msg", {textContent: ""});
const payload = %s;
delete payload.origin_shas;
GETS = [payload];
await paintRecoverySection(box, prep);
if (box.innerHTML.indexOf("추가 맥락 제공") >= 0) throw new Error("form must hide without revision hashes");
await __els["prep-rec-ctx-go"].onclick();
const msg = __els["prep-rec-msg"].textContent || "";
if (msg.indexOf("원본 바이트 정보") < 0) throw new Error("must refuse without hashes: " + msg);
if (postCalls.length !== 0) throw new Error("must not POST without revision");
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(ctx_payload())))

    def test_submit_network_failure_unable_to_confirm(self):
        check("ctxnetfail", """
const prep = %s;
const box = __mkBox(); __setEl("prep-rec", box);
__setEl("prep-rec-ctx-go", {disabled: false, onclick: null});
__setEl("prep-rec-ctx-text", {value: "메모"});
__setEl("prep-rec-msg", {textContent: ""});
box._ctxDraft = {input: "input-ctx", report: "report-ctx",
  inputSha: new Array(65).join("a"), reportSha: new Array(65).join("b"),
  text: "그대로", refs: [], focused: false};
GETS = [%s];
await paintRecoverySection(box, prep);
POSTS = [new Error("timeout")];
await __els["prep-rec-ctx-go"].onclick();
const msg = __els["prep-rec-msg"].textContent || "";
if (msg.indexOf("제출 확인 불가") < 0) throw new Error("must say unable to confirm: " + msg);
if (msg.indexOf("저장됐을 수도 있습니다") < 0) throw new Error("must warn it may have saved");
if (!box._ctxDraft || box._ctxDraft.text !== "그대로") throw new Error("draft must be kept");
if (__els["prep-rec-ctx-go"].disabled !== false) throw new Error("button must re-enable");
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(ctx_payload())))

    def test_long_context_note_full_text_and_wrap(self):
        check("ctxlong", """
const prep = %s;
const box = __mkBox(); __setEl("prep-rec", box);
const payload = %s;
payload.contexts = [{id: "context-fedcba9876543210", actor: "operator",
  question_refs: [1], text: "첫줄\\n둘째줄 " + new Array(501).join("나") + " CTX-END-z8",
  text_len: 510, created: "2026-10-29T00:00:00Z",
  origin: {input: "input-ctx", report: "report-ctx"}}];
GETS = [payload];
await paintRecoverySection(box, prep);
const html = box.innerHTML;
if (html.indexOf("CTX-END-z8") < 0) throw new Error("full note text must survive");
if (html.indexOf("첫줄<br>둘째줄") < 0) throw new Error("multiline must break");
if (html.indexOf("max-width:100%%") < 0) throw new Error("390px wrap missing");
if (html.indexOf("box-sizing:border-box") < 0) throw new Error("box sizing missing");
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(ctx_payload())))

    def test_consumed_note_survives_latest_move(self):
        check("ctxconsumed", """
const prep = %s;
const box = __mkBox(); __setEl("prep-rec", box);
const payload = %s;
payload.latest = {rid: "rr-new-B", input: "input-BBBB2222",
  status: "done", report: "report-BBBB2222",
  fingerprint: new Array(65).join("f")};
payload.origin_shas = {input: new Array(65).join("c"),
  report: new Array(65).join("d")};
payload.current_context = {id: "context-aaaa1111bbbb2222", actor: "operator",
  question_refs: [1], text: "실제 사용된 메모 MY-CONSUMED-NOTE-k7",
  text_len: 24, created: "2026-10-29T00:00:00Z",
  origin: {input: "input-AAAA1111", report: "report-AAAA1111",
    fingerprint: new Array(65).join("e"),
    input_sha256: new Array(65).join("a"),
    report_sha256: new Array(65).join("b"),
    questions: [{index: 1, text: "두 번째 질문 원문"}]},
  consumed_by: new Array(65).join("f")};
payload.contexts = [
  {id: "context-aaaa1111bbbb2222", actor: "operator",
   question_refs: [1], text: "실제 사용된 메모 MY-CONSUMED-NOTE-k7",
   text_len: 24, created: "2026-10-29T00:00:00Z",
   origin: {input: "input-AAAA1111", report: "report-AAAA1111"}},
  {id: "context-ffff999900001111", actor: "operator",
   question_refs: [], text: "다른 기록", text_len: 5,
   created: "2026-10-29T00:00:00Z",
   origin: {input: "input-AAAA1111", report: "report-AAAA1111"}}];
GETS = [payload];
await paintRecoverySection(box, prep);
const html = box.innerHTML;
["현재 검토 입력에 사용된 맥락", "MY-CONSUMED-NOTE-k7",
 "두 번째 질문 원문", "input-AAAA1111", "report-AAAA1111",
 "현재 입력 사용 1건", "현재 입력에 사용됨",
 "저장 기록 (현재 입력 사용 여부 미확인)", "input-BBBB2222"].forEach(s => {
  if (html.indexOf(s) < 0) throw new Error("missing consumed note: " + s);
});
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(ctx_payload())))


def build_ui_entry(**kw):
    entry = {"path": "package.json", "ref_kind": "candidate",
             "ref": "ab" * 20, "blob_oid": "cd" * 20,
             "raw_sha256": "12" * 32, "raw_len": 120,
             "scripts": {"build": "webpack --config x.js"},
             "omitted_scripts": [], "scripts_truncated": False,
             "sent_sha256": "34" * 32, "sent_bytes": 200,
             "text": "header"}
    entry.update(kw)
    return entry


def build_ui_payload(**kw):
    p = ctx_payload()
    p["input_build"] = [build_ui_entry()]
    p["input_build_omitted"] = []
    p.update(kw)
    return p


class TestBuildEvidenceUI(unittest.TestCase):
    def test_build_disclosure_full_and_escaped(self):
        check("buildfull", """
const prep = %s;
const box = __mkBox(); __setEl("prep-rec", box);
const payload = %s;
GETS = [payload];
await paintRecoverySection(box, prep);
const html = box.innerHTML;
["빌드 근거 3건 보기 (선언 사실·미실행)", "package.json", "candidate",
 "build", "webpack --config x.js",
 "제외 스크립트", "big", "길이 초과(2001자)",
 "example/package.json", "origin", "트리에 없음이 확인됨",
 "40000바이트", "해시 미기록(내용 미전송)", "원본 미수집(트리에 없음 확인됨)",
 "min-height:44px"].forEach(s => {
  if (html.indexOf(s) < 0) throw new Error("missing build disclosure: " + s);
});
const ref40 = "ab".repeat(20), raw64 = "12".repeat(32);
const tree40 = "ef".repeat(20), blob40 = "02".repeat(20);
[ref40, raw64, tree40, blob40].forEach(s => {
  if (html.indexOf(s) < 0) throw new Error("full digest missing: " + s.slice(0, 8));
});
if (html.indexOf("<script>") >= 0) throw new Error("malicious script text must be escaped");
if (html.indexOf("&lt;script&gt;") < 0) throw new Error("escaped payload missing");
if (html.indexOf("선언 사실 없음") >= 0) throw new Error("must not claim no declaration");
if (html.indexOf("<details") < 0) throw new Error("disclosure must be keyboard-native");
if (/width:\\d+px/.test(html)) throw new Error("no fixed pixel widths in disclosure");
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(build_ui_payload(
            input_build=[build_ui_entry(
                scripts={"build": "webpack --config x.js",
                         "evil": "<script>alert(1)</script>"},
                omitted_scripts=[{"name": "big", "kind": "long-command",
                                  "reason": "스크립트 명령 길이 초과(2001자) — 미전송"}],
                scripts_truncated=True)],
            input_build_omitted=[
                {"path": "example/package.json", "ref_kind": "origin",
                 "ref": "ef" * 20, "absent": True, "tree": "ef" * 20,
                 "reason": "읽기 가능한 고정 커밋의 트리에 없음"},
                {"path": "example/package.json", "ref_kind": "candidate",
                 "ref": "01" * 20, "blob_oid": "02" * 20, "size": 40000,
                 "reason": "빌드 근거 제외(대용량 40000바이트 > 32768바이트) — 미전송(이름만 기록; 선언 여부는 알 수 없음)"}]))))

    def test_build_legacy_shows_neutral_absence_message(self):
        check("buildlegacy", """
const prep = %s;
const box = __mkBox(); __setEl("prep-rec", box);
GETS = [%s];
await paintRecoverySection(box, prep);
let html = box.innerHTML;
if (html.indexOf("이 불변의 기존 입력은 빌드 근거를 수집하지 않았습니다") < 0) throw new Error("no neutral old-input message");
if (html.indexOf("선언된 스크립트 없음") >= 0) throw new Error("must not claim no scripts for legacy");
if (html.indexOf("저장소 스크립트가 없다는 뜻이 아닙니다") < 0) throw new Error("no repo-scripts disclaimer");
const box2 = __mkBox(); __setEl("prep-rec", box2);
const nulled = %s;
nulled.input_build = null; nulled.input_build_omitted = null;
GETS = [nulled];
await paintRecoverySection(box2, prep);
html = box2.innerHTML;
if (html.indexOf("이 불변의 기존 입력은 빌드 근거를 수집하지 않았습니다") < 0) throw new Error("explicit nulls must stay legacy");
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(ctx_payload()),
       json.dumps(ctx_payload())))

    def test_build_no_scripts_wording(self):
        check("buildwords", """
const prep = %s;
const box = __mkBox(); __setEl("prep-rec", box);
const payload = %s;
GETS = [payload];
await paintRecoverySection(box, prep);
let html = box.innerHTML;
if (html.indexOf("전송된 스크립트 없음 · 제외 1건") < 0) throw new Error("must say no transmitted scripts with omitted count");
if (html.indexOf("선언된 스크립트 없음") >= 0) throw new Error("must not claim no declaration while omitting");
const box2 = __mkBox(); __setEl("prep-rec", box2);
const empty = %s;
GETS = [empty];
await paintRecoverySection(box2, prep);
html = box2.innerHTML;
if (html.indexOf("선언된 스크립트 없음") < 0) throw new Error("genuinely empty scripts must say so");
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP),
       json.dumps(build_ui_payload(
           input_build=[build_ui_entry(
               scripts={}, omitted_scripts=[{"name": "big", "kind": "long-command",
                                             "reason": "스크립트 명령 길이 초과(2001자) — 미전송"}],
               scripts_truncated=True)])),
       json.dumps(build_ui_payload(
           input_build=[build_ui_entry(
               scripts={}, omitted_scripts=[],
               scripts_truncated=False)]))))

    def test_declaration_display_variants(self):
        with_decl = build_ui_payload()
        with_decl["report"] = full_model_report()
        with_decl["report"]["build_contract"] = {
            "builder": "npm", "command": "npm run build",
            "outputs": ["dist"],
            "declaration": {"manifest": "package.json",
                            "ref_kind": "candidate",
                            "ref": "ab" * 20, "script": "build",
                            "cwd": "", "raw_sha256": "12" * 32}}
        no_decl = build_ui_payload()
        no_decl["report"] = full_model_report()
        no_decl["report"]["build_contract"] = {
            "builder": "npm", "command": "npm run build",
            "outputs": ["dist"]}
        check("builddecl", """
const prep = %s;
const box = __mkBox(); __setEl("prep-rec", box);
const payload = %s;
GETS = [payload];
await paintRecoverySection(box, prep);
let html = box.innerHTML;
["빌드 계약", "npm run build", "선언된 출처 (미실행·모델 제안)",
 "package.json", "candidate", "build",
 "선언 원본", "선언 커밋", "(저장소 루트)"].forEach(s => {
  if (html.indexOf(s) < 0) throw new Error("missing declaration display: " + s);
});
if (html.indexOf("ab".repeat(20)) < 0) throw new Error("declaration ref must be full, not truncated");
if (html.indexOf("12".repeat(32)) < 0) throw new Error("declaration raw digest must be full");
if (html.indexOf("검증됨") >= 0) throw new Error("must never claim verified build");
const box2 = __mkBox(); __setEl("prep-rec", box2);
const nodecl = %s;
GETS = [nodecl];
await paintRecoverySection(box2, prep);
html = box2.innerHTML;
if (html.indexOf("선언된 출처") >= 0) throw new Error("must not show declaration without one");
if (html.indexOf("명령 —") < 0) throw new Error("command row must stay visible");
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(with_decl),
       json.dumps(no_decl)))


DERIVED_PREP = dict(BLOCKED_PREP)
DERIVED_PREP["op_id"] = "prep-issue-856-d1"


def derived_payload(**kw):
    base = {"op": "prep-issue-856-d1", "derived_allowed": True,
            "origin": {"input": "input-" + "a" * 16,
                       "report": "report-" + "b" * 16,
                       "fingerprint": "f" * 64},
            "origin_shas": {"input_sha256": "1" * 64,
                            "report_sha256": "2" * 64},
            "derived": []}
    base.update(kw)
    return base


def derived_item(**kw):
    item = {"dfix": "dfix-" + "c" * 32,
            "derived_from": "prep-issue-856-d1",
            "status": "started",
            "hypothesis": {"id": "H-A", "note": "가설 메모"},
            "origin": {"input": "input-" + "a" * 16,
                       "report": "report-" + "b" * 16,
                       "fingerprint": "f" * 64,
                       "base_commit": "e" * 40},
            "workspace": {"clean": True,
                          "prep_head": "d" * 40,
                          "result_head": None,
                          "applied": [],
                          "conflict_reset": [],
                          "retained_base": [],
                          "scope": {"conflicts": [],
                                    "original_touched": [],
                                    "touched": []}},
            "attempts": [], "latest_attempt": None, "job": None,
            "error": "", "updated_at": "2026-01-01T00:00:00+00:00"}
    item.update(kw)
    return item


class TestDerivedFixUI(unittest.TestCase):
    def test_not_allowed_no_form(self):
        check("dfixnoallow", """
const prep = %s;
const box = __mkBox(); __setEl("prep-derivedfix", box);
const payload = %s;
payload.derived_allowed = false;
GETS = [payload];
await paintDerivedFixSection(box, prep);
const html = box.innerHTML;
if (html.indexOf("prep-dfix-go") >= 0) throw new Error("no form expected");
if (html.indexOf("차단된 준비가 아니라") < 0) throw new Error("no reason");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload())))

    def test_no_origin_no_form(self):
        check("dfixnoorigin", """
const prep = %s;
const box = __mkBox(); __setEl("prep-derivedfix", box);
const payload = %s;
payload.origin = null;
payload.origin_shas = null;
GETS = [payload];
await paintDerivedFixSection(box, prep);
const html = box.innerHTML;
if (html.indexOf("prep-dfix-go") >= 0) throw new Error("no form expected");
if (html.indexOf("유효한 검토 원본이 없어") < 0) throw new Error("no reason");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload())))

    def test_live_shows_cancel_no_form(self):
        check("dfixlive", """
const prep = %s;
const box = __mkBox(); __setEl("prep-derivedfix", box);
const payload = %s;
payload.derived = [%s];
GETS = [payload];
await paintDerivedFixSection(box, prep);
const html = box.innerHTML;
if (html.indexOf("data-dfix-cancel") < 0) throw new Error("no cancel");
if (html.indexOf("prep-dfix-go") >= 0) throw new Error("no form while live");
if (html.indexOf("진행 중인 파생 작업") < 0) throw new Error("no busy note");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload()),
       json.dumps(derived_item(status="source-running",
                               job={"id": "job-1",
                                    "label": "derived-fix"}))))

    def test_queued_renders_waiting_no_form(self):
        check("dfixqueued", """
const prep = %s;
const box = __mkBox(); __setEl("prep-derivedfix", box);
const payload = %s;
payload.derived = [%s];
GETS = [payload];
await paintDerivedFixSection(box, prep);
const html = box.innerHTML;
if (html.indexOf("대기 중") < 0) throw new Error("no queued chip");
if (html.indexOf("prep-dfix-go") >= 0) throw new Error("no form while queued");
if (html.indexOf("대기 중인 파생 작업") < 0) throw new Error("no queued-only busy note");
if (html.indexOf("진행 중인 파생 작업") >= 0) throw new Error("queued is not running");
if (html.indexOf("대기 중 — 시작 요청이 정리") < 0) throw new Error("no honest queued note");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload()),
       json.dumps(derived_item(status="queued"))))

    def test_queued_with_job_offers_cancel(self):
        check("dfixqueuedcancel", """
const prep = %s;
const box = __mkBox(); __setEl("prep-derivedfix", box);
const payload = %s;
payload.derived = [%s];
GETS = [payload];
await paintDerivedFixSection(box, prep);
const html = box.innerHTML;
if (html.indexOf("대기 중") < 0) throw new Error("no queued chip");
if (html.indexOf("prep-dfix-go") >= 0) throw new Error("no form while queued");
if (html.indexOf("data-dfix-cancel") < 0) throw new Error("queued link must offer cancel");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload()),
       json.dumps(derived_item(status="queued",
                               job={"id": "job-9", "kind": "derived-fix",
                                    "label": "da-" + "1" * 16}))))

    def test_ready_banner_hashes_and_xss(self):
        check("dfixready", """
const prep = %s;
const box = __mkBox(); __setEl("prep-derivedfix", box);
const item = %s;
item.status = "source-ready-unverified";
item.hypothesis = {id: "H-A", note: "<script>alert(1)</script>"};
item.workspace.prep_head = "%s";
item.workspace.result_head = "%s";
item.workspace.result_tree = "%s";
item.workspace.conflict_reset = [{path: "example/webpack.config.js",
  base_blob: "%s", base_blob_sha256: "%s", absent_at_base: false,
  reason: "기록된 충돌; 베이스에 둠"}];
item.workspace.scope = {conflicts: ["example/webpack.config.js"],
  original_touched: ["example/dist/bundle.c.js"], touched: ["a", "b"]};
item.attempts = [{id: "da-" + "1".repeat(16), hypothesis_id: "H-A",
  status: "source-ready-unverified", prompt_sha256: "ab12",
  verified_paths: {"example/webpack.config.js": "%s"},
  log_bytes: 100, truncated: false, raw_total_chars: 100,
  raw_total_bytes: 100, error: "", ended_at: "t"}];
const payload = %s;
payload.derived = [item];
GETS = [payload];
await paintDerivedFixSection(box, prep);
const html = box.innerHTML;
if (html.indexOf("<script>") >= 0) throw new Error("xss");
if (html.indexOf("&lt;script&gt;") < 0) throw new Error("no escaped note");
["미검증 제안 — 빌드·실행 증명이 아님", "%s", "서버 검증 해시",
 "전송 프롬프트", "기록된 충돌 1건", "dfix-hyp"].forEach(s => {
  if (html.indexOf(s) < 0) throw new Error("missing: " + s);
});
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_item()),
       "d" * 40, "e" * 40, "f" * 40, "b" * 40, "c" * 64, "a" * 64,
       json.dumps(derived_payload()), "e" * 40))

    def test_stale_projects_stale_no_ready_banner(self):
        check("dfixstale", """
const prep = %s;
const box = __mkBox(); __setEl("prep-derivedfix", box);
const ITEMID = "dfix-" + "c".repeat(32);
const ATTID = "da-" + "1".repeat(16);
const payload = %s;
/* Real describe shape of a stale record: current administrative
   verdict + reason on top, historical ready attempt retained. */
payload.derived = [{dfix: ITEMID, derived_from: "prep-issue-856-d1",
  status: "stale", recorded_status: "stale",
  hypothesis: {id: "H-A", note: "가설 메모"},
  origin: {input: "input-" + "a".repeat(16),
    report: "report-" + "b".repeat(16), fingerprint: "f".repeat(64),
    base_commit: "e".repeat(40)},
  workspace: {clean: false, prep_head: "d".repeat(40),
    result_head: "e".repeat(40), result_tree: "f".repeat(40),
    applied: [], conflict_reset: [], retained_base: [],
    scope: {conflicts: [], original_touched: [], touched: []}},
  attempts: [{id: ATTID, hypothesis_id: "H-A",
    status: "source-ready-unverified", prompt_sha256: "ab12",
    verified_paths: {"example/webpack.config.js": "a".repeat(64)},
    log_bytes: 100, truncated: false, raw_total_chars: 100,
    raw_total_bytes: 100, error: "", ended_at: "t"}],
  latest_attempt: ATTID, job: null,
  error: "stored result failed re-verification and is marked stale",
  updated_at: "t"}];
GETS = [payload];
await paintDerivedFixSection(box, prep);
const html = box.innerHTML;
if (html.indexOf("미검증 제안 — 빌드·실행 증명이 아님") >= 0)
  throw new Error("stale must never show the ready banner");
if (html.indexOf(">stale<") < 0) throw new Error("no stale chip");
if (html.indexOf("사유:") < 0)
  throw new Error("stale must show its administrative reason");
if (html.indexOf("stored result failed re-verification") < 0)
  throw new Error("no admin stale reason text");
if (html.indexOf(ATTID) < 0)
  throw new Error("historical ready attempt must stay visible");
if (html.indexOf("서버 검증 해시") < 0)
  throw new Error("historical verified hashes must stay visible");
if (html.indexOf("prep-dfix-go") < 0)
  throw new Error("fresh derivation path must stay offered");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload())))

    def test_unknown_state_no_poll_claim(self):
        check("dfixunknown", """
const prep = %s;
const box = __mkBox(); __setEl("prep-derivedfix", box);
const payload = %s;
payload.derived = [%s];
GETS = [payload];
await paintDerivedFixSection(box, prep);
if (box.innerHTML.indexOf("소유 작업이 살아 있지 않습니다") < 0)
  throw new Error("no unknown note");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload()),
       json.dumps(derived_item(status="unknown"))))

    def test_stale_id_restore_by_stable_id(self):
        check("dfixrestore", """
const prep = %s;
const box = __mkBox(); __setEl("prep-derivedfix", box);
box.querySelector = (sel) => {
  if (sel === "#prep-dfix-note") return {value: "kept-note"};
  if (sel === "input[name='prep-dfix-hyp']:checked") return {value: "H-B"};
  return null;
};
const ITEM = "dfix-" + "c".repeat(32);
const ATT = ITEM + "-att-" + "da-" + "1".repeat(16);
box.querySelectorAll = (sel) => {
  if (sel === "[data-dfix-cancel]") return [];
  return [{getAttribute: () => ITEM, open: true},
          {getAttribute: () => ATT, open: true}];
};
const payload = %s;
payload.derived = [%s];
payload.derived[0].status = "refused";
payload.derived[0].attempts = [{id: "da-" + "1".repeat(16),
  hypothesis_id: "H-A", status: "refused", error: "no change",
  ended_at: "t"}];
GETS = [payload];
await paintDerivedFixSection(box, prep);
const html = box.innerHTML;
if (html.indexOf("prep-dfix-go") < 0)
  throw new Error("form must show for a terminal non-live record");
if (html.indexOf("kept-note") < 0) throw new Error("draft lost");
if (html.indexOf('value="H-B" checked') < 0) throw new Error("hyp lost");
if (html.indexOf('data-dfix="' + ITEM + '" open') < 0)
  throw new Error("item open state lost");
if (html.indexOf('data-dfix="' + ATT + '" open') < 0)
  throw new Error("nested attempt open state lost");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload()),
       json.dumps(derived_item())))

    def test_submit_lock_single_post_on_double_click(self):
        check("dfixlock", """
globalThis.window = {confirm: () => true};
const prep = %s;
const box = __mkBox(); __setEl("prep-derivedfix", box);
const goBtn = __mkBtn(); __setEl("prep-dfix-go", goBtn);
__setEl("prep-dfix-note", {value: "가설 메모"});
__setEl("prep-derivedfix-msg", {textContent: ""});
const payload = %s;
const fresh = JSON.parse(JSON.stringify(payload));
GETS = [payload, fresh];
POSTS = [{started: true, reused: false, attached: false,
  op: "prep-issue-856-d1", dfix: "dfix-" + "c".repeat(32),
  attempt: "da-" + "1".repeat(16), job: {id: "job-9"}}];
await paintDerivedFixSection(box, prep);
goBtn.onclick();
goBtn.onclick();
for (let i = 0; i < 25; i++) await Promise.resolve();
if (postCalls.length !== 1) throw new Error("double post: " + postCalls.length);
if (postCalls[0][1].hypothesis.note !== "가설 메모") throw new Error("note lost");
if (postCalls[0][1].context.expected.input_sha256 !== "1".repeat(64))
  throw new Error("revision not echoed");
if (pollCalls.join() !== "job-9") throw new Error("no poll on start");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload())))

    def test_origin_moved_no_rebind_no_post(self):
        check("dfixmoved", """
globalThis.window = {confirm: () => true};
const prep = %s;
const box = __mkBox(); __setEl("prep-derivedfix", box);
const goBtn = __mkBtn(); __setEl("prep-dfix-go", goBtn);
__setEl("prep-dfix-note", {value: "가설 메모"});
__setEl("prep-derivedfix-msg", {textContent: ""});
const ackBtn = __mkBtn(); __setEl("prep-dfix-ack", ackBtn);
const payload = %s;
const moved = JSON.parse(JSON.stringify(payload));
moved.origin_shas = {input_sha256: "9".repeat(64),
  report_sha256: "8".repeat(64)};
const acked = JSON.parse(JSON.stringify(moved));
const acked2 = JSON.parse(JSON.stringify(moved));
GETS = [payload, moved, acked, acked2];
await paintDerivedFixSection(box, prep);
await goBtn.onclick();
for (let i = 0; i < 25; i++) await Promise.resolve();
if (postCalls.length !== 0) throw new Error("must not post moved origin");
if ($("prep-derivedfix-msg").textContent.indexOf("원본이 바뀌었습니다") < 0)
  throw new Error("no moved-origin message");
if (box.innerHTML.indexOf("prep-dfix-go") >= 0)
  throw new Error("form must hide until explicit acknowledgement");
if (box.innerHTML.indexOf("옛 원본에 묶여 있습니다") < 0)
  throw new Error("no old-origin association");
if (box.innerHTML.indexOf("가설 메모") < 0)
  throw new Error("held note must stay visible");
await ackBtn.onclick();
for (let i = 0; i < 25; i++) await Promise.resolve();
if (postCalls.length !== 0) throw new Error("ack must not post");
if (box.innerHTML.indexOf("prep-dfix-go") < 0)
  throw new Error("form must return after acknowledgement");
if (box.innerHTML.indexOf("가설 메모") < 0)
  throw new Error("acknowledged note must survive");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload())))

    def test_contradictory_response_no_poll(self):
        check("dfixbadresp", """
globalThis.window = {confirm: () => true};
const prep = %s;
const box = __mkBox(); __setEl("prep-derivedfix", box);
const goBtn = __mkBtn(); __setEl("prep-dfix-go", goBtn);
__setEl("prep-dfix-note", {value: "가설 메모"});
__setEl("prep-derivedfix-msg", {textContent: ""});
const payload = %s;
const fresh = JSON.parse(JSON.stringify(payload));
GETS = [payload, fresh];
POSTS = [{started: true, reused: true, attached: false,
  op: "prep-issue-856-d1", dfix: "dfix-" + "c".repeat(32)}];
await paintDerivedFixSection(box, prep);
await goBtn.onclick();
for (let i = 0; i < 25; i++) await Promise.resolve();
if ($("prep-derivedfix-msg").textContent.indexOf("확인할 수 없습니다") < 0)
  throw new Error("no unknown message");
if (pollCalls.length !== 0) throw new Error("phantom poll");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload())))

    def test_retry_memory_and_button(self):
        check("dfixretry", """
globalThis.window = {confirm: () => true};
const prep = %s;
const box = __mkBox(); __setEl("prep-derivedfix", box);
const goBtn = __mkBtn(); __setEl("prep-dfix-go", goBtn);
__setEl("prep-dfix-note", {value: "메모"});
__setEl("prep-derivedfix-msg", {textContent: ""});
const payload = %s;
const fresh = JSON.parse(JSON.stringify(payload));
const failed = JSON.parse(JSON.stringify(payload));
failed.derived = [%s];
const repainted = JSON.parse(JSON.stringify(failed));
GETS = [payload, fresh, failed, repainted];
POSTS = [new Error("boom")];
await paintDerivedFixSection(box, prep);
await goBtn.onclick();
for (let i = 0; i < 25; i++) await Promise.resolve();
if (box.innerHTML.indexOf("prep-dfix-retry") < 0)
  throw new Error("no retry affordance");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload()),
       json.dumps(derived_item(status="source-error",
                               hypothesis={"id": "H-A",
                                           "note": "메모"}))))

    def test_start_renders_running_cancel_then_owned_cancel(self):
        check("dfixstartcancel", """
globalThis.window = {confirm: () => true};
const prep = %s;
const box = __mkLiveBox(); __setEl("prep-derivedfix", box);
const goBtn = __mkBtn(); __setEl("prep-dfix-go", goBtn);
__setEl("prep-dfix-note", {value: "가설 메모"});
__setEl("prep-derivedfix-msg", {textContent: ""});
const ITEMID = "dfix-" + "c".repeat(32);
const ATTID = "da-" + "1".repeat(16);
const empty = %s;
const fresh = JSON.parse(JSON.stringify(empty));
const live = JSON.parse(JSON.stringify(empty));
live.derived = [{dfix: ITEMID, derived_from: "prep-issue-856-d1",
  status: "source-running", hypothesis: {id: "H-A", note: "가설 메모"},
  origin: {input: "input-" + "a".repeat(16),
    report: "report-" + "b".repeat(16), fingerprint: "f".repeat(64),
    base_commit: "e".repeat(40)},
  workspace: {clean: false, prep_head: "d".repeat(40),
    result_head: null, applied: [], conflict_reset: [],
    retained_base: [], scope: {conflicts: [], original_touched: [],
    touched: []}},
  attempts: [{id: ATTID, hypothesis_id: "H-A",
    status: "source-running"}], latest_attempt: ATTID,
  job: {id: "job-9", kind: "derived-fix", label: ATTID},
  error: "", updated_at: "t"}];
GETS = [empty, fresh, live];
POSTS = [{started: true, reused: false, attached: false,
  op: "prep-issue-856-d1", dfix: ITEMID, attempt: ATTID,
  job: {id: "job-9", kind: "derived-fix", label: ATTID}},
  {cancelled: true, dfix: ITEMID}];
await paintDerivedFixSection(box, prep);
await goBtn.onclick();
for (let i = 0; i < 25; i++) await Promise.resolve();
if (postCalls.length !== 1) throw new Error("one start post expected");
if ($("prep-derivedfix-msg").textContent.indexOf("파생 수정 시작됨") < 0)
  throw new Error("no started message");
if (box.innerHTML.indexOf("prep-dfix-go") >= 0)
  throw new Error("form must hide while running");
if (box.innerHTML.indexOf('data-dfix-cancel="' + ITEMID + '"') < 0)
  throw new Error("cancel button must appear promptly after start");
if (pollCalls.join() !== "job-9") throw new Error("must poll the job");
if (__timers.length < 1) throw new Error("terminal poll must be scheduled");
/* Only the CURRENT render's controls exist: re-query after the
   observable start repaint, then use the NEW visible cancel node.
   A stale paint's closure could never be re-armed here. */
const cur = box.querySelectorAll("[data-dfix-cancel]");
if (cur.length !== 1) throw new Error("one live cancel expected");
if (cur[0].getAttribute("data-dfix-cancel") !== ITEMID)
  throw new Error("cancel must name the owned record");
if (typeof cur[0].onclick !== "function") throw new Error("no fresh cancel handler");
await cur[0].onclick();
for (let i = 0; i < 25; i++) await Promise.resolve();
if (postCalls.length !== 2) throw new Error("cancel must post once");
if (postCalls[1][0] !== "/api/integration/derived-fix/cancel")
  throw new Error("wrong cancel route");
if (postCalls[1][1].dfix !== ITEMID || postCalls[1][1].confirm !== true)
  throw new Error("cancel must name only the owned record");
if ($("prep-derivedfix-msg").textContent.indexOf("취소 요청됨") < 0)
  throw new Error("no cancelled message");
const term = JSON.parse(JSON.stringify(live));
term.derived[0].status = "source-ready-unverified";
term.derived[0].attempts = [{id: ATTID, hypothesis_id: "H-A",
  status: "source-ready-unverified"}];
GETS.push(term); GETS.push(JSON.parse(JSON.stringify(term)));
if (__timers.length < 1) throw new Error("no terminal poll scheduled");
const polls = __timers.filter((t) => t && t.__timerMs === 5000);
if (polls.length !== 1)
  throw new Error("exactly one terminal poll must be scheduled");
const waits = __timers.filter((t) => t && t.__timerMs === 15000);
if (waits.length < 1)
  throw new Error("bounded preflight wait must stay scheduled");
const n0 = getCalls;
GETS.push(term); GETS.push(JSON.parse(JSON.stringify(term)));
await polls[0]();
for (let i = 0; i < 40; i++) await Promise.resolve();
if (getCalls !== n0 + 3)
  throw new Error("terminal poll must fetch once then redraw section+preflight");
if (box.innerHTML.indexOf("미검증 제안 — 빌드·실행 증명이 아님") < 0)
  throw new Error("no terminal redraw after running->terminal poll");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload())))

    def test_attach_renders_running_and_polls_terminal(self):
        check("dfixattach", """
globalThis.window = {confirm: () => true};
const prep = %s;
const box = __mkBox(); __setEl("prep-derivedfix", box);
const goBtn = __mkBtn(); __setEl("prep-dfix-go", goBtn);
__setEl("prep-dfix-note", {value: "가설 메모"});
__setEl("prep-derivedfix-msg", {textContent: ""});
box.querySelectorAll = (sel) => [];
const payload = %s;
const fresh = JSON.parse(JSON.stringify(payload));
const live = JSON.parse(JSON.stringify(payload));
live.derived = [{dfix: "dfix-" + "c".repeat(32),
  derived_from: "prep-issue-856-d1", status: "source-running",
  hypothesis: {id: "H-A", note: "가설 메모"},
  origin: {input: "input-" + "a".repeat(16),
    report: "report-" + "b".repeat(16), fingerprint: "f".repeat(64),
    base_commit: "e".repeat(40)},
  workspace: {}, attempts: [], latest_attempt: null,
  job: {id: "job-2", kind: "derived-fix",
    label: "da-" + "1".repeat(16)},
  error: "", updated_at: "t"}];
GETS = [payload, fresh, live];
POSTS = [{started: false, reused: true, attached: true,
  op: "prep-issue-856-d1", dfix: "dfix-" + "c".repeat(32),
  job: {id: "job-2", kind: "derived-fix",
    label: "da-" + "1".repeat(16)}}];
await paintDerivedFixSection(box, prep);
await goBtn.onclick();
for (let i = 0; i < 25; i++) await Promise.resolve();
if ($("prep-derivedfix-msg").textContent.indexOf("연결합니다") < 0)
  throw new Error("no attach message");
if (box.innerHTML.indexOf("prep-dfix-go") >= 0)
  throw new Error("form must hide while attached running");
if (box.innerHTML.indexOf("data-dfix-cancel") < 0)
  throw new Error("cancel must render on attach");
if (__timers.length < 1)
  throw new Error("attach must schedule a terminal poll");
const apolls = __timers.filter((t) => t && t.__timerMs === 5000);
if (apolls.length !== 1)
  throw new Error("exactly one terminal poll must be scheduled");
const awaits = __timers.filter((t) => t && t.__timerMs === 15000);
if (awaits.length < 1)
  throw new Error("bounded preflight wait must stay scheduled");
const term = JSON.parse(JSON.stringify(live));
term.derived[0].status = "cancelled";
term.derived[0].attempts = [{id: "da-" + "1".repeat(16),
  hypothesis_id: "H-A", status: "cancelled"}];
const n0 = getCalls;
GETS.push(term); GETS.push(JSON.parse(JSON.stringify(term)));
await apolls[0]();
for (let i = 0; i < 40; i++) await Promise.resolve();
if (getCalls !== n0 + 3)
  throw new Error("attach poll must fetch once then redraw section+preflight");
if (box.innerHTML.indexOf("data-dfix-cancel") >= 0)
  throw new Error("cancel must disappear after terminal redraw");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload())))

    def test_hypothesis_labels_match_server_contract(self):
        defs = server_hyp_defs()
        check("dfixhyplabels", """
const prep = %s;
const box = __mkBox(); __setEl("prep-derivedfix", box);
const payload = %s;
const DEFS = %s;
GETS = [payload];
await paintDerivedFixSection(box, prep);
const html = box.innerHTML;
for (const id of Object.keys(DEFS)) {
  if (html.indexOf(id + " — " + DEFS[id]) < 0)
    throw new Error(id + " label must equal the server definition");
}
if (html.indexOf("소유자 정책 답변이 아님") < 0)
  throw new Error("operator-note must disclaim owner answers");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload()),
       json.dumps(defs)))

    def test_preflight_verified_panel_shows_channels_and_authority(self):
        check("pfverified", """
const prep = %s;
const box = __mkBox(); __setEl("prep-derivedfix", box);
const payload = %s;
const PFID = "pf-" + "1".repeat(16);
const chan = (observed, positive, detail) => ({required: "restricted",
  observed: observed, positive: positive, detail: detail});
const pf = {state: "verified", pfid: PFID,
  verified_at: "2026-01-01T00:00:00+00:00",
  inputs_sha256: "a".repeat(64),
  binary: {path: "<img src=x>", sha256: "b".repeat(64)},
  config_status: "unestablished",
  auth: {meta_api_key_present: false,
    credential_source: "owned-fixture-observer"},
  channels: {shell: chan("restricted", true, "관측됨: 시작 표식"),
    web: chan("restricted", true, "관측됨"),
    mcp: chan("restricted", true, "관측됨"),
    hook: chan("restricted", true, "관측됨"),
    network: chan("unknown", false, "관측 없음")},
  authority_now: {authorized: true, pfid: PFID,
    verified_at: "2026-01-01T00:00:00+00:00"},
  reason: "5개 채널 중 4개 확인, 네트워크 미확인",
  history: [{pfid: PFID, state: "verified",
    reason: "5개 채널 중 4개 확인"}]};
GETS = [payload, {__for: "preflight", body: pf}];
await paintDerivedFixSection(box, prep);
const html = box.innerHTML;
if (html.indexOf("격리 점검 — 확인됨") < 0)
  throw new Error("no verified title");
for (const w of ["셸 명령", "웹 접근", "외부 도구 연결",
    "자동 실행 훅", "네트워크", "제한 사용", "확인됨",
    "미확인 (소스 작업 시작 불가)", "muse-spark-1.3", "현재 권한: 유효",
    "a".repeat(64), "b".repeat(64), "owned-fixture-observer",
    "data-pf-run", "data-pf-cancel", "data-pf-create"])
  if (w[0] === "d" ? html.indexOf(w) >= 0 : html.indexOf(w) < 0)
    throw new Error("panel proof missing or action leaked: " + w);
if (html.indexOf("<img") >= 0) throw new Error("binary path xss");
if (html.indexOf("&lt;img") < 0) throw new Error("no escaped path");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload())))

    def test_preflight_authority_expired_is_honest(self):
        check("pfexpired", """
const prep = %s;
const box = __mkBox(); __setEl("prep-derivedfix", box);
const payload = %s;
const PFID = "pf-" + "2".repeat(16);
const chan = (observed, positive) => ({required: "restricted",
  observed: observed, positive: positive, detail: ""});
const pf = {state: "verified", pfid: PFID,
  verified_at: "2026-01-01T00:00:00+00:00",
  inputs_sha256: "c".repeat(64),
  binary: {path: "/bin/muse", sha256: "d".repeat(64)},
  config_status: "unestablished",
  auth: {meta_api_key_present: false, credential_source: "x"},
  channels: {shell: chan("restricted", true),
    web: chan("restricted", true), mcp: chan("restricted", true),
    hook: chan("restricted", true), network: chan("unknown", false)},
  authority_now: {authorized: false, code: "source-drift",
    reason: "원본이 바뀌었습니다. 다시 점검하세요."},
  reason: "ok", history: []};
GETS = [payload, {__for: "preflight", body: pf}];
await paintDerivedFixSection(box, prep);
const html = box.innerHTML;
if (html.indexOf("현재 권한: 만료 또는 변경됨") < 0)
  throw new Error("no expired authority line");
if (html.indexOf("source-drift") < 0)
  throw new Error("no authority code");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload())))

    def test_preflight_running_offers_cancel_only(self):
        check("pfrunning", """
globalThis.window = {confirm: () => true};
const prep = %s;
const box = __mkBox(); __setEl("prep-derivedfix", box);
__setEl("prep-derivedfix-msg", {textContent: ""});
const payload = %s;
const PFID = "pf-" + "3".repeat(16);
const livePf = {state: "running", pfid: PFID,
  reason: "probe running", code: "running",
  history: [{pfid: PFID, state: "running", reason: "probe running"}],
  channels: {},
  authority_now: {authorized: false, code: "running",
    reason: "probe running"}};
const cancelBtn = __mkBtn();
cancelBtn.getAttribute = (k) =>
  (k === "data-pf-cancel" ? "pf-" + "3".repeat(16) : null);
box.querySelectorAll = (sel) =>
  (sel === "[data-pf-cancel]" ? [cancelBtn] : []);
GETS = [payload, {__for: "preflight", body: livePf}];
await paintDerivedFixSection(box, prep);
if (typeof cancelBtn.onclick !== "function")
  throw new Error("cancel must be wired while running");
if (box.innerHTML.indexOf("격리 점검 — 진행 중") < 0)
  throw new Error("no running title");
if (box.innerHTML.indexOf("data-pf-run") >= 0)
  throw new Error("run must be absent while running");
const donePf = {state: "not-verified", pfid: null,
  reason: "cancelled", code: "no-preflight", history: [],
  channels: {},
  authority_now: {authorized: false, code: "no-preflight",
    reason: "cancelled"}};
const fresh = JSON.parse(JSON.stringify(payload));
GETS.push(fresh, {__for: "preflight", body: donePf});
POSTS = [{state: "cancelled", pfid: PFID}];
await cancelBtn.onclick();
for (let i = 0; i < 25; i++) await Promise.resolve();
if (postCalls.length !== 1) throw new Error("cancel must post once");
if (postCalls[0][0] !== "/api/integration/derived-fix/preflight/cancel")
  throw new Error("wrong cancel route");
if (postCalls[0][1].pfid !== PFID || postCalls[0][1].confirm !== true)
  throw new Error("cancel must name the live check only");
if ($("prep-derivedfix-msg").textContent.indexOf("점검 취소됨") < 0)
  throw new Error("no cancelled message");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload())))

    def test_pending_preflight_times_out_without_hiding_controls(self):
        check("pfpending", """
const prep = %s;
const box = __mkBox(); __setEl("prep-derivedfix", box);
const payload = %s;
const PFID = "pf-" + "5".repeat(16);
const slowPf = {state: "created", pfid: PFID,
  reason: "created late", code: "", history: [],
  channels: {},
  authority_now: {authorized: false, code: "no-preflight",
    reason: "created"}};
GETS = [payload, {__for: "preflight", __deferred: true,
  body: slowPf}];
const t0 = __timers.length;
const g0 = getCalls;
const first = paintDerivedFixSection(box, prep);
for (let i = 0; i < 10; i++) await Promise.resolve();
// Both reads were actually requested; the preflight one stays
// outstanding while the derived section is already known.
if (getCalls !== g0 + 2)
  throw new Error("section and preflight reads must be requested");
const pfPending = __pendingGets.filter(
  (e) => String(e.url).indexOf("derived-fix/preflight") >= 0);
if (pfPending.length !== 1)
  throw new Error("exactly one preflight read must stay outstanding");
if (__timers.length !== t0 + 1)
  throw new Error("one bounded preflight wait must be scheduled");
const waits = __timers.filter((t) => t && t.__timerMs === 15000);
if (waits.length !== 1)
  throw new Error("exactly one bounded preflight wait must be pending");
// Fire the bounded wait manually (the harness never runs timers):
// a slow preflight must resolve to an honest timeout reason with
// the derived controls committed — never hang on empty DOM.
waits[0]();
await first;
for (let i = 0; i < 25; i++) await Promise.resolve();
const html = box.innerHTML;
if (html.indexOf("prep-dfix-go") < 0)
  throw new Error("derived start control must render after timeout");
if (html.indexOf("prep-dfix-note") < 0)
  throw new Error("derived draft control must render after timeout");
if (html.indexOf("조회 시간 초과") < 0)
  throw new Error("slow preflight must report an honest timeout");
if (postCalls.length !== 0)
  throw new Error("a timed-out read must not start any model job");
// Release the leftover deferred read so later tests see a clean
// pending queue; the completed paint ignores the late answer.
pfPending[0].resolve();
for (let i = 0; i < 10; i++) await Promise.resolve();
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload())))

    def test_preflight_create_preserves_derived_draft(self):
        check("pfdraft", TRACKBOX_JS + """
const prep = %s;
const calls = [];
const box = __mkTrackBox(calls);
__setEl("prep-derivedfix", box);
__setEl("prep-derivedfix-msg", {textContent: ""});
globalThis.document = {activeElement: null, body: {}};
// Serve the preflight create button from CURRENT markup on top of
// the tracked controls: the product wires it during paint.
const rawQSA = box.querySelectorAll.bind(box);
// Same node identity within one markup generation (like the
// tracked controls): the product wires onclick during paint, and
// the test must click THAT wired object, not a fresh copy.
let pfCacheHtml = null, pfCacheNodes = null;
box.querySelectorAll = (sel) => {
  if (sel === "[data-pf-create]") {
    if (pfCacheHtml !== box.innerHTML) {
      pfCacheHtml = box.innerHTML;
      pfCacheNodes = [];
      const re = /<button\\b[^>]*data-pf-create="1"[^>]*>/g;
      let m;
      while ((m = re.exec(box.innerHTML))) {
        pfCacheNodes.push(
          {disabled: /\\bdisabled\\b/.test(m[0]), onclick: null,
           getAttribute: (k) =>
             (k === "data-pf-create" ? "1" : null)});
      }
    }
    return pfCacheNodes;
  }
  return rawQSA(sel);
};
const payload = %s;
const absentPf = {state: "absent", pfid: null,
  reason: "no verified isolation check", code: "no-preflight",
  history: [], channels: {},
  authority_now: {authorized: false, code: "no-preflight",
    reason: "no verified isolation check"}};
GETS = [payload, {__for: "preflight", body: absentPf}];
await paintDerivedFixSection(box, prep);
for (let i = 0; i < 25; i++) await Promise.resolve();
// Type into the CURRENT rendered textarea (faithful state): the
// product's readKept reads box.querySelector only, never a
// detached global element.
const ta = box.querySelector("#prep-dfix-note");
if (!ta || typeof ta.value !== "string")
  throw new Error("no rendered textarea");
ta.value = "임시 <b>초안</b>";
const btns = box.querySelectorAll("[data-pf-create]");
if (btns.length !== 1 || typeof btns[0].onclick !== "function")
  throw new Error("create must be wired");
const PFID = "pf-" + "4".repeat(16);
const createdPf = {state: "created", pfid: PFID,
  reason: "created", code: "", history: [{pfid: PFID, state: "created",
  reason: "created"}], channels: {},
  authority_now: {authorized: false, code: "no-preflight",
    reason: "created"}};
const fresh = JSON.parse(JSON.stringify(payload));
GETS.push(fresh, {__for: "preflight", body: createdPf});
POSTS = [{pfid: PFID, reused: false, state: "created"}];
await btns[0].onclick();
for (let i = 0; i < 25; i++) await Promise.resolve();
if (postCalls.length !== 1) throw new Error("create must post once");
if (postCalls[0][0] !== "/api/integration/derived-fix/preflight/create")
  throw new Error("wrong create route");
const html = box.innerHTML;
if (html.indexOf("임시 &lt;b&gt;초안&lt;/b&gt;") < 0)
  throw new Error("draft lost across preflight repaint");
if (html.indexOf("임시 <b>초안</b>") >= 0)
  throw new Error("draft markup must stay escaped");
if (html.indexOf("격리 점검") < 0)
  throw new Error("panel missing after repaint");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload())))

    def test_typing_during_delayed_get_is_preserved(self):
        check("dfixtypedelay", """
const prep = %s;
const box = __mkBox(); __setEl("prep-derivedfix", box);
__setEl("prep-derivedfix-msg", {textContent: ""});
const ITEM = "dfix-" + "c".repeat(32);
const ATT = ITEM + "-att-" + "da-" + "1".repeat(16);
/* Mutable DOM model: the test drives typing, radio selection and
   open/close DURING the outstanding GET, like a real user. */
const dom = {note: "", hyp: "H-A", open: [ITEM], formOpen: true};
box.querySelector = (sel) => {
  if (sel === "#prep-dfix-note") return {get value() { return dom.note; }, set value(v) { dom.note = String(v); }};
  if (sel === "input[name='prep-dfix-hyp']:checked") return {get value() { return dom.hyp; }};
  if (sel === "details[data-dfix-form]") return {get open() { return dom.formOpen; }};
  return null;
};
box.querySelectorAll = (sel) => {
  if (sel === "[data-dfix-cancel]") return [];
  if (sel === "details[data-dfix][open]") return dom.open.map((id) => ({getAttribute: () => id, open: true}));
  return [];
};
const base = %s;
base.derived = [%s];
base.derived[0].status = "refused";
base.derived[0].attempts = [{id: "da-" + "1".repeat(16),
  hypothesis_id: "H-A", status: "refused", error: "no change",
  ended_at: "t"}];
GETS = [{__deferred: true, payload: base}];
const first = paintDerivedFixSection(box, prep);
for (let i = 0; i < 5; i++) await Promise.resolve();
if (getCalls !== 1) throw new Error("GET must have been requested");
if (__pendingGets.length !== 1) throw new Error("GET must stay outstanding");
if (String(__pendingGets[0].url).indexOf("prep-issue-856-d1") < 0)
  throw new Error("wrong GET url");
/* While the GET is outstanding: type, switch radio, expand the
   nested attempt. */
dom.note = "typed-during-get";
dom.hyp = "H-B";
dom.open = [ITEM, ATT];
__pendingGets[0].resolve();
await first;
for (let i = 0; i < 25; i++) await Promise.resolve();
const html = box.innerHTML;
if (html.indexOf("typed-during-get") < 0)
  throw new Error("typing during a pending GET must survive repaint");
if (html.indexOf('value="H-B" checked') < 0)
  throw new Error("radio selection during a pending GET must survive");
if (html.indexOf('data-dfix="' + ITEM + '" open') < 0)
  throw new Error("record expansion must survive a pending GET");
if (html.indexOf('data-dfix="' + ATT + '" open') < 0)
  throw new Error("nested attempt expansion during a pending GET must survive");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload()),
       json.dumps(derived_item())))

    def test_pending_get_close_last_and_form_close_stay_closed(self):
        check("dfixpendingclose", """
const prep = %s;
const box = __mkBox(); __setEl("prep-derivedfix", box);
__setEl("prep-derivedfix-msg", {textContent: ""});
const ITEM = "dfix-" + "c".repeat(32);
const ATT = ITEM + "-att-" + "da-" + "1".repeat(16);
const dom = {note: "draft-before-get", hyp: "H-A", open: [ITEM, ATT], formOpen: true};
box.querySelector = (sel) => {
  if (sel === "#prep-dfix-note") return {get value() { return dom.note; }, set value(v) { dom.note = String(v); }};
  if (sel === "input[name='prep-dfix-hyp']:checked") return {get value() { return dom.hyp; }};
  if (sel === "details[data-dfix-form]") return {get open() { return dom.formOpen; }};
  return null;
};
box.querySelectorAll = (sel) => {
  if (sel === "[data-dfix-cancel]") return [];
  if (sel === "details[data-dfix][open]") return dom.open.map((id) => ({getAttribute: () => id, open: true}));
  return [];
};
const base = %s;
base.derived = [%s];
base.derived[0].status = "refused";
base.derived[0].attempts = [{id: "da-" + "1".repeat(16),
  hypothesis_id: "H-A", status: "refused", error: "no change",
  ended_at: "t"}];
GETS = [{__deferred: true, payload: base}];
const first = paintDerivedFixSection(box, prep);
for (let i = 0; i < 5; i++) await Promise.resolve();
if (getCalls !== 1 || __pendingGets.length !== 1)
  throw new Error("GET must be requested and outstanding");
/* While outstanding: close the last accordion and the form, keep
   editing the draft. The repaint must honor the newest DOM — no
   silent reopen. */
dom.open = [];
dom.formOpen = false;
dom.note = "typed-still-here";
__pendingGets[0].resolve();
await first;
for (let i = 0; i < 25; i++) await Promise.resolve();
const html = box.innerHTML;
if (html.indexOf("typed-still-here") < 0)
  throw new Error("draft must survive closing everything during GET");
if (html.indexOf('data-dfix="' + ITEM + '" open') >= 0)
  throw new Error("closed record must not reopen after pending GET");
if (html.indexOf('data-dfix="' + ATT + '" open') >= 0)
  throw new Error("closed attempt must not reopen after pending GET");
if (html.indexOf("data-dfix-form>") < 0)
  throw new Error("form must stay closed after pending GET");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload()),
       json.dumps(derived_item())))

    def test_stale_op_timer_touches_nothing(self):
        check("dfixstaletimer", """
globalThis.window = {confirm: () => true};
const prep = %s;
const box = __mkBox(); __setEl("prep-derivedfix", box);
const goBtn = __mkBtn(); __setEl("prep-dfix-go", goBtn);
__setEl("prep-dfix-note", {value: "가설 메모"});
__setEl("prep-derivedfix-msg", {textContent: ""});
box.querySelectorAll = (sel) => [];
const payload = %s;
const fresh = JSON.parse(JSON.stringify(payload));
const live = JSON.parse(JSON.stringify(payload));
live.derived = [{dfix: "dfix-" + "c".repeat(32),
  derived_from: "prep-issue-856-d1", status: "source-running",
  hypothesis: {id: "H-A", note: "가설 메모"},
  origin: {input: "input-" + "a".repeat(16),
    report: "report-" + "b".repeat(16), fingerprint: "f".repeat(64),
    base_commit: "e".repeat(40)},
  workspace: {}, attempts: [], latest_attempt: null,
  job: {id: "job-2", kind: "derived-fix",
    label: "da-" + "1".repeat(16)},
  error: "", updated_at: "t"}];
GETS = [payload, fresh, live];
POSTS = [{started: false, reused: true, attached: true,
  op: "prep-issue-856-d1", dfix: "dfix-" + "c".repeat(32),
  job: {id: "job-2", kind: "derived-fix",
    label: "da-" + "1".repeat(16)}}];
await paintDerivedFixSection(box, prep);
await goBtn.onclick();
for (let i = 0; i < 25; i++) await Promise.resolve();
if (__timers.length < 1) throw new Error("no live poll scheduled");
// The operator moved to another op: the box generation changes.
box.dataset.dFixOp = "prep-issue-999-d9";
box.dataset.dFixSeq = "4242";
const htmlBefore = box.innerHTML;
const n0 = getCalls;
// Fire the actual terminal poll (5000ms), never the 15000ms
// preflight bound: the stale-op guard must drop the fetch.
const stalePolls = __timers.filter((t) => t && t.__timerMs === 5000);
if (stalePolls.length !== 1)
  throw new Error("exactly one terminal poll must be scheduled");
await stalePolls[0]();
for (let i = 0; i < 25; i++) await Promise.resolve();
if (getCalls !== n0)
  throw new Error("a stale-op timer must not fetch");
if (box.innerHTML !== htmlBefore)
  throw new Error("a stale-op timer must not redraw");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload())))


def full_input_view(**kw):
    base = {"dfix": "dfix-" + "c" * 32,
            "attempt_id": "da-" + "1" * 16,
            "status": "stored",
            "prompt_sha256": "p" * 64, "prompt_bytes": 42,
            "pin_sha256": "q" * 64, "pin_bytes": 900,
            "source_refs": {"candidate_head": "c" * 40,
                            "base_head": "b" * 40,
                            "prep_head": "d" * 40,
                            "prep_tree": "e" * 40},
            "omitted": [],
            "evidence": [],
            "profile": {"provider": "meta", "model": "muse-spark-1.3",
                        "contributor": "on",
                        "subscription": "login-session",
                        "timeout_s": 600,
                        "binary_sha256": "not-collected",
                        "toolset_doc_sha256": "not-collected",
                        "toolset_doc_version": "not-collected",
                        "capacity": "unknown",
                        "argv_state": "requested",
                        "argv_note": "n", "workdir": "/tmp/w"},
            "questions": {"total": 2, "sent": 2, "omitted": 0,
                          "clipped": 0, "clipped_bytes": 0},
            "conflicts": [], "original_touched": [],
            "text": "line1\nline2"}
    base.update(kw)
    return base


def viewer_item():
    item = derived_item(status="source-ready-unverified")
    item["attempts"] = [{"id": "da-" + "1" * 16,
                         "hypothesis_id": "H-A",
                         "status": "source-ready-unverified",
                         "full_input_state": "stored-unverified",
                         "prompt_sha256": "p" * 64}]
    return item


class TestFullInputViewerUI(unittest.TestCase):
    def test_stored_full_text_and_provenance(self):
        check("fxstored", """
const prep = %s;
const box = __mkBox(); __setEl("prep-derivedfix", box);
const payload = %s;
payload.derived = [%s];
box._dFixInputSel = {dfix: "dfix-" + "c".repeat(32),
  att: "da-" + "1".repeat(16)};
const view = %s;
GETS = [payload, view];
await paintDerivedFixSection(box, prep);
const html = box.innerHTML;
if (html.indexOf("전체 전송 입력 — 저장됨") < 0)
  throw new Error("no stored panel");
if (html.indexOf("첫줄") < 0 || html.indexOf("둘째줄") < 0)
  throw new Error("full text missing");
if (html.indexOf("&lt;b&gt;") < 0)
  throw new Error("text must be escaped");
if (html.indexOf("<b>not-tag</b>") >= 0)
  throw new Error("raw markup must not render");
if (html.indexOf("p".repeat(64)) < 0)
  throw new Error("prompt sha missing");
if (html.indexOf("질문 2/3건 전송 (그 중 1건 잘림 · 100바이트 생략)") < 0)
  throw new Error("clipped question counts missing");
if (html.indexOf("고정 참조에 없음(확인됨)") < 0)
  throw new Error("absent reason missing");
if (html.indexOf("전체 한도로 축소 전송") < 0)
  throw new Error("budget reason missing");
if (html.indexOf("미전송 1건 · 부분 전송 1건") < 0)
  throw new Error("full vs partial counts must be distinguished");
if (html.indexOf("코드 미전송") < 0)
  throw new Error("fully omitted row must not claim bytes sent");
if (html.indexOf("12B 전송됨(일부)") < 0)
  throw new Error("partial row must show delivered bytes");
if (html.indexOf("data-input-key=") < 0)
  throw new Error("stable panel key missing");
if (html.indexOf("이 텍스트가 앱이 보낸 전체 입력") < 0)
  throw new Error("scope note missing");
if (html.indexOf("muse-spark-1.3") < 0)
  throw new Error("requested model missing");
if (postCalls.length !== 0)
  throw new Error("viewer must never POST");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload()),
       json.dumps(viewer_item()), json.dumps(full_input_view(
           text="첫줄\n둘째줄 <b>not-tag</b>",
           questions={"total": 3, "sent": 2, "omitted": 1,
                      "clipped": 1, "clipped_bytes": 100},
           omitted=[{"path": "example/webpack.config.js",
                     "ref_kind": "candidate",
                     "reason": "absent-at-ref", "sent_bytes": 30},
                    {"path": "src/__tests__/webpack-web.test.ts",
                     "ref_kind": "prepared",
                     "reason": "over-global-budget",
                     "sent_bytes": 12}]))))

    def test_viewer_xss_escaped(self):
        check("fxxss", """
const prep = %s;
const box = __mkBox(); __setEl("prep-derivedfix", box);
const payload = %s;
payload.derived = [%s];
box._dFixInputSel = {dfix: "dfix-" + "c".repeat(32),
  att: "da-" + "1".repeat(16)};
const view = %s;
GETS = [payload, view];
await paintDerivedFixSection(box, prep);
const html = box.innerHTML;
if (html.indexOf("XSSM&lt;script&gt;") < 0)
  throw new Error("payload text must be escaped");
if (html.indexOf("XSSM<script>") >= 0)
  throw new Error("raw payload script must not render");
if (html.indexOf('P&lt;img src=x&gt;') < 0)
  throw new Error("payload path must be escaped");
if (html.indexOf('P<img src=x>') >= 0)
  throw new Error("raw payload img must not render");
if (html.indexOf('M&quot;&gt;&lt;script&gt;') < 0)
  throw new Error("payload model must be escaped");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload()),
       json.dumps(viewer_item()), json.dumps(full_input_view(
           text="XSSM<script>alert(1)</script>",
           omitted=[{"path": "P<img src=x>", "ref_kind": "base",
                     "reason": "absent-at-ref", "sent_bytes": 5}],
           profile={"provider": "meta",
                    "model": 'M"><script>',
                    "contributor": "on",
                    "subscription": "login-session",
                    "timeout_s": 600,
                    "binary_sha256": "not-collected",
                    "toolset_doc_sha256": "not-collected",
                    "toolset_doc_version": "not-collected",
                    "capacity": "unknown",
                    "argv_state": "requested",
                    "argv_note": "n", "workdir": "/tmp/w"}))))

    def test_viewer_statuses_errors_empty(self):
        check("fxstatus", """
const prep = %s;
const base = %s;
const item = %s;
async function one(view, label, extra) {
  const box = __mkBox(); __setEl("prep-derivedfix", box);
  const payload = JSON.parse(JSON.stringify(base));
  payload.derived = [JSON.parse(JSON.stringify(item))];
  box._dFixInputSel = {dfix: "dfix-" + "c".repeat(32),
    att: "da-" + "1".repeat(16)};
  GETS = [payload, view];
  await paintDerivedFixSection(box, prep);
  const html = box.innerHTML;
  if (html.indexOf(label) < 0)
    throw new Error("missing label " + label);
  if (extra && html.indexOf(extra) < 0)
    throw new Error("missing detail " + extra);
}
await one({status: "legacy-not-recorded"}, "기록 없음");
await one({status: "not-yet-dispatched"}, "미전송");
await one({status: "pre-input-failure", error: "원인이다"},
  "전송 전 실패", "원인이다");
await one({status: "missing-required"}, "보관분실");
await one({status: "weird-status", error: ""}, "검증 실패");
await one("oops", "조회 실패");
await one({status: "stored", prompt_sha256: "p".repeat(64),
  prompt_bytes: 0, pin_sha256: "q".repeat(64), pin_bytes: 7,
  source_refs: {}, omitted: [], evidence: [],
  profile: {}, questions: {}, text: ""}, "저장됨");
await one({status: "stored", prompt_sha256: "p".repeat(64),
  prompt_bytes: 3, pin_sha256: "q".repeat(64), pin_bytes: 7,
  source_refs: {}, omitted: [], evidence: [],
  profile: {}, questions: {}, text: "Ex"}, "저장됨", "Ex");
console.log("PASS");
const prep2 = %s;
const box2 = __mkBox(); __setEl("prep-derivedfix", box2);
const p2 = JSON.parse(JSON.stringify(base));
p2.derived = [JSON.parse(JSON.stringify(item))];
box2._dFixInputSel = {dfix: "dfix-" + "c".repeat(32),
  att: "da-" + "1".repeat(16)};
GETS = [p2, new Error("net down")];
await paintDerivedFixSection(box2, prep2);
if (box2.innerHTML.indexOf("조회 실패") < 0)
  throw new Error("GET rejection must render 조회 실패");
if (box2.innerHTML.indexOf("net down") < 0)
  throw new Error("GET reason must be shown");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload()),
       json.dumps(viewer_item()), json.dumps(DERIVED_PREP)))

    def test_viewer_loading_visible_accessible(self):
        check("fxloading", """
const prep = %s;
const box = __mkBox(); __setEl("prep-derivedfix", box);
const payload = %s;
payload.derived = [%s];
box._dFixInputSel = {dfix: "dfix-" + "c".repeat(32),
  att: "da-" + "1".repeat(16)};
const view = %s;
GETS = [payload, {__deferred: true, payload: view}];
const first = paintDerivedFixSection(box, prep);
for (let i = 0; i < 10; i++) await Promise.resolve();
if (__pendingGets.length !== 1)
  throw new Error("input GET must stay outstanding");
const loading = box.innerHTML;
// An honest visible accessible loading state, not an empty box:
// the section content stays while the input view loads.
if (loading.indexOf("data-dfix-input-panel") < 0)
  throw new Error("loading panel must render while waiting");
if (loading.indexOf("읽는 중") < 0)
  throw new Error("loading text must be visible");
if (loading.indexOf('aria-busy="true"') < 0)
  throw new Error("loading must expose aria-busy");
if (loading.indexOf('role="status"') < 0)
  throw new Error("loading must expose a status role");
if (loading.indexOf("파생 소스 수정") < 0)
  throw new Error("existing section content must stay while loading");
if (loading.indexOf("disabled") < 0)
  throw new Error("opening button must disable while its view loads");
if (loading.indexOf("전체 전송 입력 — 저장됨") >= 0)
  throw new Error("no stored content before the GET resolves");
__pendingGets[0].resolve();
await first;
for (let i = 0; i < 10; i++) await Promise.resolve();
if (box.innerHTML.indexOf("전체 전송 입력 — 저장됨") < 0)
  throw new Error("panel must render after resolve");
if (box.innerHTML.indexOf("읽는 중") >= 0)
  throw new Error("loading must clear after resolve");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload()),
       json.dumps(viewer_item()), json.dumps(full_input_view())))

    def test_viewer_stale_race_ignored(self):
        check("fxrace", """
const prep = %s;
const box = __mkBox(); __setEl("prep-derivedfix", box);
const payload = %s;
payload.derived = [%s];
const viewA = %s;
const viewB = %s;
box._dFixInputSel = {dfix: "dfix-" + "c".repeat(32),
  att: "da-" + "a".repeat(16)};
GETS = [payload, {__deferred: true, payload: viewA}];
const p1 = paintDerivedFixSection(box, prep);
for (let i = 0; i < 5; i++) await Promise.resolve();
box._dFixInputSel = {dfix: "dfix-" + "c".repeat(32),
  att: "da-" + "b".repeat(16)};
GETS.push(payload, {__deferred: true, payload: viewB});
const p2 = paintDerivedFixSection(box, prep);
for (let i = 0; i < 5; i++) await Promise.resolve();
if (__pendingGets.length !== 2)
  throw new Error("both input GETs must be outstanding");
__pendingGets[0].resolve();
await p1;
for (let i = 0; i < 10; i++) await Promise.resolve();
if (box.innerHTML.indexOf("TEXT-A") >= 0)
  throw new Error("stale response must not render");
__pendingGets[1].resolve();
await p2;
for (let i = 0; i < 10; i++) await Promise.resolve();
if (box.innerHTML.indexOf("TEXT-B") < 0)
  throw new Error("latest response must render");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload()),
       json.dumps(viewer_item()),
       json.dumps(full_input_view(text="TEXT-A")),
       json.dumps(full_input_view(text="TEXT-B"))))

    def test_viewer_close_clears_without_extra_get(self):
        check("fxclose", """
const prep = %s;
const box = __mkBox(); __setEl("prep-derivedfix", box);
const payload = %s;
payload.derived = [%s];
box._dFixInputSel = {dfix: "dfix-" + "c".repeat(32),
  att: "da-" + "1".repeat(16)};
const view = %s;
GETS = [payload, view];
await paintDerivedFixSection(box, prep);
if (box.innerHTML.indexOf("data-dfix-input-panel") < 0)
  throw new Error("panel must render while selected");
const n0 = getCalls;
box._dFixInputSel = null;
GETS.push(payload);
await paintDerivedFixSection(box, prep);
if (box.innerHTML.indexOf("data-dfix-input-panel") >= 0)
  throw new Error("panel must clear after close");
if (getCalls !== n0 + 2)
  throw new Error("close must reread section+preflight, not the input view");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload()),
       json.dumps(viewer_item()), json.dumps(full_input_view())))

    def test_viewer_keyboard_wrap_hooks(self):
        check("fxa11y", """
const prep = %s;
const box = __mkBox(); __setEl("prep-derivedfix", box);
const payload = %s;
payload.derived = [%s];
box._dFixInputSel = {dfix: "dfix-" + "c".repeat(32),
  att: "da-" + "1".repeat(16)};
GETS = [payload, %s];
await paintDerivedFixSection(box, prep);
const html = box.innerHTML;
// String-level hooks only: real 390px layout and screen-reader
// acceptance stay a later browser gate, never inferred here.
if ((html.match(/min-height:44px/g) || []).length < 2)
  throw new Error("viewer controls need >=44px targets");
if (html.indexOf("<summary") < 0)
  throw new Error("native keyboard disclosure missing");
if (html.indexOf("data-dfix-input-panel") < 0)
  throw new Error("stable panel hook missing");
if (html.indexOf("data-dfix-input-close") < 0)
  throw new Error("close hook missing");
if (html.indexOf("pre-wrap") < 0 ||
    html.indexOf("word-break:break-word") < 0 ||
    html.indexOf("max-width:100%%") < 0)
  throw new Error("wrap hooks missing for narrow viewports");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload()),
       json.dumps(viewer_item()), json.dumps(full_input_view())))

    def test_attempt_label_uses_input_state(self):
        check("fxlabel", """
const prep = %s;
const box = __mkBox(); __setEl("prep-derivedfix", box);
const payload = %s;
payload.derived = [%s];
GETS = [payload];
await paintDerivedFixSection(box, prep);
const html = box.innerHTML;
if (html.indexOf("전체 입력: 저장됨(미검증)") < 0)
  throw new Error("attempt input-state label missing");
if (html.indexOf("전체 전송 입력 보기") < 0)
  throw new Error("viewer button missing");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload()),
       json.dumps(viewer_item())))

    def test_second_await_preserves_newest_edits(self):
        check("fxsecond", """
const prep = %s;
const box = __mkBox(); __setEl("prep-derivedfix", box);
const payload = %s;
payload.derived = [%s];
box._dFixInputSel = {dfix: "dfix-" + "c".repeat(32),
  att: "da-" + "1".repeat(16)};
const view = %s;
// Live DOM the painter reads through box.querySelector(All).
const dom = {
  note: {value: "초안v1"},
  hypA: {value: "H-A"},
  hypChecked: null,
  accordOpen: ["d-ncsr"],
  formOpen: true,
  panelOpen: true,
};
box.querySelector = (sel) => {
  if (sel === "#prep-dfix-note") return dom.note;
  if (sel === "input[name='prep-dfix-hyp']:checked") return dom.hypChecked;
  if (sel === "details[data-dfix-form]") return {get open() { return dom.formOpen; }};
  if (sel === "details[data-dfix-input-panel]") return {
    get open() { return dom.panelOpen; },
    set open(v) { dom.panelOpen = !!v; },
    getAttribute: (k) => (k === "data-input-key"
      ? "prep-issue-856-d1/dfix-" + "c".repeat(32) + "/da-" + "1".repeat(16) : null)};
  return null;
};
box.querySelectorAll = (sel) => {
  if (sel === "details[data-dfix][open]")
    return dom.accordOpen.map((id) => ({getAttribute: () => id}));
  return [];
};
GETS = [payload, {__deferred: true, payload: view}];
const first = paintDerivedFixSection(box, prep);
for (let i = 0; i < 10; i++) await Promise.resolve();
// The user edits DURING the second wait: newest text, other
// hypothesis, accordion closed, form + input panel closed.
dom.note.value = "초안v2-최종";
dom.hypChecked = {value: "H-B"};
dom.accordOpen = [];
dom.formOpen = false;
dom.panelOpen = false;
__pendingGets[0].resolve();
await first;
for (let i = 0; i < 10; i++) await Promise.resolve();
const html = box.innerHTML;
if (html.indexOf("초안v2-최종") < 0)
  throw new Error("newest draft must survive the second wait");
if (html.indexOf("초안v1") >= 0)
  throw new Error("stale draft must not overwrite newest edits");
if (html.indexOf('value="H-B" checked') < 0)
  throw new Error("changed hypothesis must survive");
if (html.indexOf("data-dfix-input-panel") < 0)
  throw new Error("input panel must render");
if (html.indexOf('data-input-key="prep-issue-856-d1/dfix-'
    + "c".repeat(32) + "/da-" + "1".repeat(16) + '"') < 0)
  throw new Error("panel must carry the stable op/dfix/attempt key");
if (/data-dfix-input-panel[^>]* open>/.test(html))
  throw new Error("user-closed input panel must stay closed");
if (/data-dfix-form[^>]* open>/.test(html))
  throw new Error("user-closed form must stay closed");
if (/data-dfix="[^"]*" open>/.test(html))
  throw new Error("user-closed accordions must stay closed");
if (html.indexOf("전체 전송 입력 — 저장됨") < 0)
  throw new Error("resolved input view must render stored content");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload()),
       json.dumps(viewer_item()), json.dumps(full_input_view())))

    def test_error_codes_map_to_distinct_panels(self):
        check("fxcodes", """
const prep = %s;
const base = %s;
const item = %s;
async function one(code, message, label, absent) {
  const box = __mkBox(); __setEl("prep-derivedfix", box);
  const payload = JSON.parse(JSON.stringify(base));
  payload.derived = [JSON.parse(JSON.stringify(item))];
  box._dFixInputSel = {dfix: "dfix-" + "c".repeat(32),
    att: "da-" + "1".repeat(16)};
  const err = new Error(message);
  err.code = code;
  GETS = [payload, err];
  await paintDerivedFixSection(box, prep);
  const html = box.innerHTML;
  if (html.indexOf(label) < 0)
    throw new Error("missing panel for code " + code);
  if (absent && html.indexOf(absent) >= 0)
    throw new Error("wrong panel content for code " + code);
}
// A lost artifact is 보관분실, never a fetch failure or stored.
await one("missing-required", "gone", "보관분실", "조회 실패");
// Corruption is 검증 실패, never presented as stored content.
await one("corrupt", "pin mismatch", "검증 실패", "전체 전송 입력 — 저장됨");
// Anything else (and empty codes) stay 조회 실패.
await one("", "net down", "조회 실패", null);
await one("weird-code", "boom", "조회 실패", null);
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload()),
       json.dumps(viewer_item())))

    def test_failed_fetch_rereads_on_repaint(self):
        check("fxreread", """
const prep = %s;
const box = __mkBox(); __setEl("prep-derivedfix", box);
const payload = %s;
payload.derived = [%s];
box._dFixInputSel = {dfix: "dfix-" + "c".repeat(32),
  att: "da-" + "1".repeat(16)};
const err = new Error("net down");
err.code = "";
GETS = [payload, err];
await paintDerivedFixSection(box, prep);
if (box.innerHTML.indexOf("조회 실패") < 0)
  throw new Error("failure must render until refresh");
const n0 = getCalls;
// A repaint (explicit reread or terminal poll) re-reads the
// immutable input; no model retry is ever issued.
const view = %s;
GETS = [payload, view];
await paintDerivedFixSection(box, prep);
if (getCalls !== n0 + 3)
  throw new Error("repaint must re-read section+preflight+input view");
if (postCalls.length !== 0)
  throw new Error("repaint must not POST or retry the model");
if (box.innerHTML.indexOf("전체 전송 입력 — 저장됨") < 0)
  throw new Error("recovered read must render stored content");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload()),
       json.dumps(viewer_item()), json.dumps(full_input_view())))

    def test_click_flow_open_close_focus(self):
        check("fxclick", TRACKBOX_JS + """
const prep = %s;
const calls = [];
const box = __mkTrackBox(calls);
__setEl("prep-derivedfix", box);
globalThis.document = {activeElement: null, body: {}};
const payload = %s;
payload.derived = [%s];
const view = %s;
const DFIX = "dfix-" + "c".repeat(32);
const ATT = "da-" + "1".repeat(16);
GETS = [payload];
await paintDerivedFixSection(box, prep);
let opens = box.querySelectorAll("[data-dfix-input]");
if (opens.length !== 1 || typeof opens[0].onclick !== "function")
  throw new Error("open button must be wired on the real controls");
// Real advertised click: select, repaint, GET, panel, focus.
globalThis.document.activeElement = opens[0];
GETS.push(payload, view);
await opens[0].onclick();
for (let i = 0; i < 25; i++) await Promise.resolve();
if (!box._dFixInputSel || box._dFixInputSel.att !== ATT)
  throw new Error("click must record the selection");
if (box.innerHTML.indexOf("전체 전송 입력 — 저장됨") < 0)
  throw new Error("click must render the stored view");
const KEY = "prep-issue-856-d1/" + DFIX + "/" + ATT;
if (box.innerHTML.indexOf('data-input-key="' + KEY + '"') < 0)
  throw new Error("rendered panel must carry the stable key");
if (calls.indexOf("panel-focus") < 0)
  throw new Error("explicit open must move focus to the panel");
// Disclosure state survives a repaint: the user closes the actual
// current panel element (no external boolean survives innerHTML).
box.querySelector("details[data-dfix-input-panel]").open = false;
GETS.push(payload, view);
await paintDerivedFixSection(box, prep);
for (let i = 0; i < 25; i++) await Promise.resolve();
if (box.innerHTML.indexOf("data-dfix-input-panel") < 0)
  throw new Error("panel must persist across repaint");
if (/data-dfix-input-panel[^>]* open>/.test(box.innerHTML))
  throw new Error("closed panel must stay closed");
// Explicit open reopens even the closed same selection (force).
opens = box.querySelectorAll("[data-dfix-input]");
globalThis.document.activeElement = opens[0];
GETS.push(payload, view);
await opens[0].onclick();
for (let i = 0; i < 25; i++) await Promise.resolve();
if (!/data-dfix-input-panel[^>]* open>/.test(box.innerHTML))
  throw new Error("explicit open must reopen the closed panel");
// Real close click: selection clears, no panel, focus returns to
// the open button. The close repaint rereads only the section,
// never the recorded input bytes.
const closes = box.querySelectorAll("[data-dfix-input-close]");
if (closes.length !== 1 || typeof closes[0].onclick !== "function")
  throw new Error("close button must be wired");
const n0 = getCalls;
globalThis.document.activeElement = closes[0];
GETS.push(payload);
await closes[0].onclick();
for (let i = 0; i < 25; i++) await Promise.resolve();
if (box._dFixInputSel !== null)
  throw new Error("close must clear the selection");
if (box.innerHTML.indexOf("data-dfix-input-panel") >= 0)
  throw new Error("panel must clear after close");
if (getCalls !== n0 + 2)
  throw new Error("close repaint rereads section+preflight, not input");
if (calls.filter((c) => c === "open-focus:" + ATT).length < 1)
  throw new Error("close must return focus to the open button");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload()),
       json.dumps(viewer_item()), json.dumps(full_input_view())))

    def test_loading_phase_controls_work_during_pending_read(self):
        check("fxpending", TRACKBOX_JS + """
const prep = %s;
const calls = [];
const box = __mkTrackBox(calls);
__setEl("prep-derivedfix", box);
globalThis.document = {activeElement: null, body: {}};
const payload = %s;
const item = %s;
const DFIX = "dfix-" + "c".repeat(32);
const ATTA = "da-" + "1".repeat(16);
const ATTB = "da-" + "2".repeat(16);
item.attempts = [
  {id: ATTA, hypothesis_id: "H-A", status: "source-ready-unverified",
   full_input_state: "stored-unverified", prompt_sha256: "a".repeat(64)},
  {id: ATTB, hypothesis_id: "H-B", status: "source-ready-unverified",
   full_input_state: "stored-unverified", prompt_sha256: "b".repeat(64)}];
payload.derived = [item];
const viewA = %s;
const viewB = %s;
box._dFixInputSel = {dfix: DFIX, att: ATTA};
GETS = [payload, {__deferred: true, payload: viewA}];
const p1 = paintDerivedFixSection(box, prep);
let n = 0;
while (box.innerHTML.indexOf("읽는 중") < 0 && n++ < 500) await Promise.resolve();
if (box.innerHTML.indexOf("읽는 중") < 0)
  throw new Error("loading panel must commit before the input read");
// The loading-phase open control for the OTHER attempt is already
// wired and enabled: clicking it during the pending read must work
// and must not start a model job.
const btnB = box.querySelectorAll("[data-dfix-input]")
  .filter((b) => b.getAttribute("data-att") === ATTB)[0];
if (!btnB || typeof btnB.onclick !== "function")
  throw new Error("loading-phase controls must be wired during pending read");
if (btnB.disabled !== false)
  throw new Error("the other attempt must stay clickable during loading");
GETS.push(payload, viewB);
globalThis.document.activeElement = btnB;
await btnB.onclick();
for (let i = 0; i < 25; i++) await Promise.resolve();
// The stale first selection resolves late: it must never return
// closed content or disturb the newer selection.
__pendingGets[0].resolve();
await p1;
for (let i = 0; i < 25; i++) await Promise.resolve();
if (!box._dFixInputSel || box._dFixInputSel.att !== ATTB)
  throw new Error("newer selection must win over a stale response");
const html = box.innerHTML;
if (html.indexOf("BETA-VIEW") < 0)
  throw new Error("newer stored view must render");
if (html.indexOf("ALPHA-VIEW") >= 0)
  throw new Error("stale response must never return closed content");
if (html.indexOf('data-input-key="prep-issue-856-d1/' + DFIX + "/" + ATTB + '"') < 0)
  throw new Error("panel key must follow the newer selection");
if (box._dFixInputLoading !== null)
  throw new Error("loading flag must clear after completion");
if (postCalls.length !== 0)
  throw new Error("opening a stored view must never POST");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload()),
       json.dumps(viewer_item()),
       json.dumps(full_input_view(text="ALPHA-VIEW")),
       json.dumps(full_input_view(text="BETA-VIEW"))))

    def test_open_focus_tab_away_keeps_panel_without_focus(self):
        check("fxfocusaway", TRACKBOX_JS + """
const prep = %s;
const calls = [];
const box = __mkTrackBox(calls);
__setEl("prep-derivedfix", box);
const foreign = {id: "elsewhere"};
globalThis.document = {activeElement: foreign, body: {}};
const payload = %s;
payload.derived = [%s];
const view = %s;
GETS = [payload];
await paintDerivedFixSection(box, prep);
const opens = box.querySelectorAll("[data-dfix-input]");
// The user tabbed elsewhere before the click resolves: the panel
// still opens, but focus must not be stolen back.
GETS.push(payload, view);
await opens[0].onclick();
for (let i = 0; i < 25; i++) await Promise.resolve();
if (box.innerHTML.indexOf("전체 전송 입력 — 저장됨") < 0)
  throw new Error("panel must render even when focus moved away");
if (calls.indexOf("panel-focus") >= 0)
  throw new Error("focus must not be stolen after the user tabbed away");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload()),
       json.dumps(viewer_item()), json.dumps(full_input_view())))

    def test_stale_open_response_never_steals_focus(self):
        check("fxfocusreorder", TRACKBOX_JS + """
const prep = %s;
const calls = [];
const box = __mkTrackBox(calls);
__setEl("prep-derivedfix", box);
globalThis.document = {activeElement: null, body: {}};
const payload = %s;
const item = %s;
const DFIX = "dfix-" + "c".repeat(32);
const ATTA = "da-" + "1".repeat(16);
const ATTB = "da-" + "2".repeat(16);
item.attempts = [
  {id: ATTA, hypothesis_id: "H-A", status: "source-ready-unverified",
   full_input_state: "stored-unverified", prompt_sha256: "a".repeat(64)},
  {id: ATTB, hypothesis_id: "H-B", status: "source-ready-unverified",
   full_input_state: "stored-unverified", prompt_sha256: "b".repeat(64)}];
payload.derived = [item];
const viewA = %s;
const viewB = %s;
box._dFixInputSel = {dfix: DFIX, att: ATTA};
GETS = [payload, {__deferred: true, payload: viewA}];
const pA = paintDerivedFixSection(box, prep);
let n = 0;
while (box.innerHTML.indexOf("읽는 중") < 0 && n++ < 500) await Promise.resolve();
// While A is pending, the user opens B: B completes first.
const btnB = box.querySelectorAll("[data-dfix-input]")
  .filter((b) => b.getAttribute("data-att") === ATTB)[0];
GETS.push(payload, viewB);
globalThis.document.activeElement = btnB;
await btnB.onclick();
for (let i = 0; i < 25; i++) await Promise.resolve();
// The stale A response arrives late: no second focus move, no
// content swap, selection stays on B.
__pendingGets[0].resolve();
await pA;
for (let i = 0; i < 25; i++) await Promise.resolve();
if (calls.filter((c) => c === "panel-focus").length !== 1)
  throw new Error("only the explicit newer open may move focus");
if (!box._dFixInputSel || box._dFixInputSel.att !== ATTB)
  throw new Error("selection must stay on the newer attempt");
const html = box.innerHTML;
if (html.indexOf("ORDER-B") < 0 || html.indexOf("ORDER-A") >= 0)
  throw new Error("stale content must never replace the newer view");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload()),
       json.dumps(viewer_item()),
       json.dumps(full_input_view(text="ORDER-A")),
       json.dumps(full_input_view(text="ORDER-B"))))

    def test_typing_caret_survives_delayed_repaint(self):
        check("fxcaret", TRACKBOX_JS + """
const prep = %s;
const calls = [];
const box = __mkTrackBox(calls);
__setEl("prep-derivedfix", box);
globalThis.document = {activeElement: null, body: {}};
Object.defineProperty(__els, "prep-dfix-note",
  {get: () => box.querySelector("#prep-dfix-note"), configurable: true});
const payload = %s;
GETS = [payload];
await paintDerivedFixSection(box, prep);
// The user types in the rendered form, with caret + focus.
const note = box.querySelector("#prep-dfix-note");
if (!note) throw new Error("form textarea must render");
note.value = "caret-draft-12345";
note.selectionStart = 6; note.selectionEnd = 11;
globalThis.document.activeElement = note;
// A delayed reread repaints: newest text, caret, and focus must
// move to the new node — the old detached node is not reused.
GETS = [{__deferred: true, payload: payload}];
const p = paintDerivedFixSection(box, prep);
let n = 0;
while (__pendingGets.length !== 1 && n++ < 500) await Promise.resolve();
if (__pendingGets.length !== 1) throw new Error("reread GET must be outstanding");
__pendingGets[0].resolve();
await p;
for (let i = 0; i < 25; i++) await Promise.resolve();
const now = box.querySelector("#prep-dfix-note");
if (!now || now.value !== "caret-draft-12345")
  throw new Error("typed draft must survive the delayed reread");
if (now === note)
  throw new Error("repaint must recreate nodes, not reuse detached ones");
if (calls.indexOf("focus:prep-dfix-note") < 0)
  throw new Error("focus must return to the textarea after repaint");
if (calls.indexOf("sel:6:11") < 0)
  throw new Error("caret range must be restored on the new node");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload())))

    def test_real_jget_propagates_error_code_end_to_end(self):
        src = HTML.read_text(encoding="utf-8")
        found = re.findall(r"^\s*(?:async\s+)?function\s+jget\s*\(",
                           src, re.M)
        self.assertEqual(len(found), 1,
                         "exactly one product jget must exist")
        real = extract("jget", src)
        self.assertIn("err.code", real)
        renamed = real.replace("async function jget(",
                               "async function realJget(", 1)
        self.assertIn("async function realJget(", renamed)
        check("fxjget", TRACKBOX_JS + renamed + """
const prep = %s;
const calls = [];
const box = __mkTrackBox(calls);
__setEl("prep-derivedfix", box);
globalThis.document = {activeElement: null, body: {}};
const payload = %s;
payload.derived = [%s];
const DFIX = "dfix-" + "c".repeat(32);
const ATT = "da-" + "1".repeat(16);
box._dFixInputSel = {dfix: DFIX, att: ATT};
const view = %s;
// Controlled fetch: the section read succeeds; the input route
// answers per mode (ok / typed HTTP error / untyped HTTP error).
let fetchMode = "ok";
const fetchUrls = [];
globalThis.fetch = async (u) => {
  fetchUrls.push(String(u));
  if (String(u).indexOf("/api/integration/derived-fix/input?") >= 0) {
    if (fetchMode === "missing")
      return {ok: false, status: 502, statusText: "Bad Gateway",
        json: async () => ({error: "gone", code: "missing-required"})};
    if (fetchMode === "weird")
      return {ok: false, status: 502, statusText: "Bad Gateway",
        json: async () => ({})};
    return {ok: true, status: 200, json: async () => view};
  }
  return {ok: true, status: 200, json: async () => payload};
};
// Rewire to the ACTUAL extracted product fetcher for this test.
jget = realJget;
// Direct boundary: typed HTTP errors keep message AND code.
fetchMode = "missing";
const e1 = await jget("http://127.0.0.1:1/api/integration/derived-fix/input?op=o").catch((e) => e);
if (!e1 || e1.message !== "gone")
  throw new Error("real jget must surface the HTTP error message");
if (e1.code !== "missing-required")
  throw new Error("real jget must propagate the typed error code");
// End to end through the real viewer: stored renders, the exact
// route is fetched, typed codes map to distinct panels, and an
// untyped failure never claims stored content.
fetchMode = "ok";
fetchUrls.length = 0;
await paintDerivedFixSection(box, prep);
for (let i = 0; i < 25; i++) await Promise.resolve();
if (box.innerHTML.indexOf("전체 전송 입력 — 저장됨") < 0)
  throw new Error("stored view must render through real jget");
if (!fetchUrls.some((u) => u.indexOf("/api/integration/derived-fix/input?op=") >= 0))
  throw new Error("viewer must query the real input route");
if (!fetchUrls.some((u) => u.indexOf(DFIX) >= 0 && u.indexOf(ATT) >= 0))
  throw new Error("input query must carry the selected dfix/attempt");
fetchMode = "missing";
await paintDerivedFixSection(box, prep);
for (let i = 0; i < 25; i++) await Promise.resolve();
if (box.innerHTML.indexOf("전체 전송 입력 — 보관분실") < 0)
  throw new Error("missing-required must render the lost panel");
fetchMode = "weird";
await paintDerivedFixSection(box, prep);
for (let i = 0; i < 25; i++) await Promise.resolve();
const h = box.innerHTML;
if (h.indexOf("전체 전송 입력 — 조회 실패") < 0)
  throw new Error("untyped failure must render the fetch-failure panel");
if (h.indexOf("전체 전송 입력 — 저장됨") >= 0 || h.indexOf("전체 전송 입력 — 보관분실") >= 0 || h.indexOf("전체 전송 입력 — 검증 실패") >= 0)
  throw new Error("a fetch failure must never claim stored/lost/corrupt content");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload()),
       json.dumps(viewer_item()), json.dumps(full_input_view())))

    def test_submit_malformed_revision_hashes_refuse_without_post(self):
        check("ctxbadshasubmit", """
const prep = %s;
const box = __mkBox(); __setEl("prep-rec", box);
__setEl("prep-rec-ctx-go", {disabled: false, onclick: null});
__setEl("prep-rec-ctx-text", {value: "운용자 메모"});
__setEl("prep-rec-msg", {textContent: ""});
const bads = ["z", new Array(64).join("a"), new Array(65).join("A"),
  " " + new Array(64).join("b"), new Array(64).join("c") + " ", 17, null];
for (const bad of bads) {
  GETS = [%s];
  POSTS = [];
  postCalls.length = 0;
  await paintRecoverySection(box, prep);
  // Tampered/stale client revision state: both hash fields must be
  // exact lowercase hex even though the painted chain is complete.
  // Short, uppercase, and whitespace-padded digests all refuse.
  box._ctxShas = {input: bad, report: bad};
  if (typeof __els["prep-rec-ctx-go"].onclick !== "function")
    throw new Error("submit must be wired");
  await __els["prep-rec-ctx-go"].onclick();
  const msg = __els["prep-rec-msg"].textContent || "";
  if (msg.indexOf("원본 바이트 정보") < 0)
    throw new Error("must refuse malformed revision: " + bad);
  if (postCalls.length !== 0)
    throw new Error("must not POST malformed revision: " + bad);
  if (__els["prep-rec-ctx-text"].value !== "운용자 메모")
    throw new Error("textarea draft must be kept: " + bad);
  if (__els["prep-rec-ctx-go"].disabled !== false)
    throw new Error("button must stay enabled after refusal: " + bad);
}
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP), json.dumps(ctx_payload())))

    def test_first_get_disclosure_toggle_respected(self):
        check("fxfirsttoggle", TRACKBOX_JS + """
const prep = %s;
const calls = [];
const box = __mkTrackBox(calls);
__setEl("prep-derivedfix", box);
globalThis.document = {activeElement: null, body: {}};
const payload = %s;
payload.derived = [%s];
const DFIX = "dfix-" + "c".repeat(32);
const ATT = "da-" + "1".repeat(16);
const KEY = "prep-issue-856-d1/" + DFIX + "/" + ATT;
const view = %s;
box._dFixInputSel = {dfix: DFIX, att: ATT};
GETS = [payload, view];
await paintDerivedFixSection(box, prep);
for (let i = 0; i < 25; i++) await Promise.resolve();
if (!/data-dfix-input-panel[^>]* open>/.test(box.innerHTML))
  throw new Error("panel starts open for a fresh selection");
// A reread with a slow FIRST fetch: the user closes the actual
// current same-key disclosure while waiting. The loading commit
// after that fetch must honor the newest toggle, not resurrect
// the starting-state snapshot.
GETS = [{__deferred: true, payload: payload},
  {__deferred: true, payload: view}];
const p = paintDerivedFixSection(box, prep);
let n = 0;
while (__pendingGets.length !== 1 && n++ < 500) await Promise.resolve();
if (__pendingGets.length !== 1) throw new Error("first GET must be outstanding");
box.querySelector("details[data-dfix-input-panel]").open = false;
__pendingGets[0].resolve();
n = 0;
while ((box.innerHTML.indexOf("읽는 중") < 0 || __pendingGets.length !== 2) && n++ < 500) await Promise.resolve();
if (box.innerHTML.indexOf("읽는 중") < 0)
  throw new Error("loading panel must commit after the first fetch");
if (/data-dfix-input-panel[^>]* open>/.test(box.innerHTML))
  throw new Error("loading must respect the toggle made during the first wait");
if (box.innerHTML.indexOf('id="dfix-input-panel-summary"') < 0)
  throw new Error("loading summary keeps the stable id for keyboard focus");
// The user reopens during the SECOND wait: the final panel must
// open, keyed to the same selection.
box.querySelector("details[data-dfix-input-panel]").open = true;
__pendingGets[1].resolve();
await p;
for (let i = 0; i < 25; i++) await Promise.resolve();
const html = box.innerHTML;
if (!/data-dfix-input-panel[^>]* open>/.test(html))
  throw new Error("final panel must respect the toggle made during the second wait");
if (html.indexOf('data-input-key="' + KEY + '"') < 0)
  throw new Error("panel key must survive both phases");
if (html.indexOf("전체 전송 입력 — 저장됨") < 0)
  throw new Error("resolved view must render stored content");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload()),
       json.dumps(viewer_item()), json.dumps(full_input_view())))

    def test_explicit_open_then_close_during_second_wait(self):
        check("fxforceconsume", TRACKBOX_JS + """
const prep = %s;
const calls = [];
const box = __mkTrackBox(calls);
__setEl("prep-derivedfix", box);
globalThis.document = {activeElement: null, body: {}};
const payload = %s;
payload.derived = [%s];
const DFIX = "dfix-" + "c".repeat(32);
const ATT = "da-" + "1".repeat(16);
const view = %s;
GETS = [payload];
await paintDerivedFixSection(box, prep);
let opens = box.querySelectorAll("[data-dfix-input]");
// Explicit open click force-opens the loading disclosure; the input
// read stays pending. Closing it during the second wait must win:
// force is consumed by the loading commit, never resurrected.
GETS.push(payload, {__deferred: true, payload: view});
globalThis.document.activeElement = opens[0];
const pClick = opens[0].onclick();
let n = 0;
while (box.innerHTML.indexOf("읽는 중") < 0 && n++ < 500) await Promise.resolve();
if (!/data-dfix-input-panel[^>]* open>/.test(box.innerHTML))
  throw new Error("explicit open must force the loading panel open");
if (box.innerHTML.indexOf('id="dfix-input-panel-summary"') < 0)
  throw new Error("loading summary keeps the stable id for keyboard focus");
box.querySelector("details[data-dfix-input-panel]").open = false;
__pendingGets[0].resolve();
await pClick;
for (let i = 0; i < 25; i++) await Promise.resolve();
if (!box._dFixInputSel || box._dFixInputSel.att !== ATT)
  throw new Error("selection must remain recorded");
const html = box.innerHTML;
if (/data-dfix-input-panel[^>]* open>/.test(html))
  throw new Error("user close during the second wait must win over force-open");
if (html.indexOf("전체 전송 입력 — 저장됨") < 0)
  throw new Error("resolved content must still render while closed");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload()),
       json.dumps(viewer_item()), json.dumps(full_input_view())))

    def test_build_omitted_states_truthful(self):
        omitted = [
            {"path": "example/package.json", "ref_kind": "candidate",
             "ref": "ab" * 20, "tree": "ab" * 20, "absent": True,
             "reason": "읽기 가능한 고정 커밋의 트리에 없음"},
            {"path": "package.json", "ref_kind": "candidate",
             "ref": "ab" * 20, "blob_oid": "cd" * 20, "size": 777001,
             "reason": ("빌드 근거 제외(대용량 777001바이트 > 32768바이트)"
                        " — 미전송(이름만 기록; 선언 여부는 알 수 없음)")},
            {"path": "example/package.json", "ref_kind": "base",
             "ref": "ef" * 20, "blob_oid": "12" * 20, "size": 500,
             "reason": ("빌드 근거 제외(스크립트 수 초과: 99건)"
                        " — 미전송(이름만 기록; 선언 여부는 알 수 없음)")},
            {"path": "x.json", "ref_kind": "candidate",
             "reason": "unknown-shape"}]
        check("ctxbomit", """
const prep = %s;
const box = __mkBox(); __setEl("prep-rec", box);
const payload = %s;
GETS = [payload];
await paintRecoverySection(box, prep);
const html = box.innerHTML;
if (html.indexOf("원본 미수집(트리에 없음 확인됨)") < 0)
  throw new Error("confirmed absence must be explicit");
if (html.indexOf("해시 미기록(내용 미전송)") < 0)
  throw new Error("oversize omission must say hash unrecorded");
if (html.indexOf("원본 읽고 파싱됨 · 해시 미기록(내용 미전송)") < 0)
  throw new Error("read-and-parsed omission must be distinguished");
if (html.indexOf("777001바이트") < 0)
  throw new Error("omitted size must render in bytes");
if (html.indexOf("777001자") >= 0)
  throw new Error("Git byte sizes must not render as chars");
if (html.indexOf("출처 미확인(내용 미전송)") < 0)
  throw new Error("unknown omission shape must stay unclaimed");
if (html.indexOf("raw 미수집") >= 0)
  throw new Error("must not claim raw was never collected");
console.log("PASS");
""" % (json.dumps(BLOCKED_PREP),
       json.dumps(build_ui_payload(input_build_omitted=omitted))))

    def test_trackbox_lifecycle_semantics(self):
        check("fxtracklife", TRACKBOX_JS + """
const prep = %s;
const calls = [];
const box = __mkTrackBox(calls);
__setEl("prep-derivedfix", box);
globalThis.document = {activeElement: null, body: {}};
const payload = %s;
payload.derived = [%s];
const DFIX = "dfix-" + "c".repeat(32);
const ATT = "da-" + "1".repeat(16);
box._dFixInputSel = {dfix: DFIX, att: ATT};
const view = %s;
GETS = [payload, view];
await paintDerivedFixSection(box, prep);
for (let i = 0; i < 25; i++) await Promise.resolve();
// Same generation: identical node objects across both query APIs,
// and current markup determines initial open/checked state.
const opensA = box.querySelectorAll("[data-dfix-input]");
const opensB = box.querySelectorAll("[data-dfix-input]");
if (opensA.length !== 1 || opensA[0] !== opensB[0])
  throw new Error("same generation must return identical nodes");
if (box.querySelector("[data-dfix-input]") !== opensA[0])
  throw new Error("querySelector must agree with querySelectorAll");
const panelA = box.querySelector("details[data-dfix-input-panel]");
if (!panelA || panelA.open !== true)
  throw new Error("fresh selection renders the panel open");
const radioA = box.querySelector("input[name='prep-dfix-hyp']:checked");
if (!radioA || radioA.value !== "H-A")
  throw new Error("checked radio comes from current markup");
// Connected focus moves activeElement and logs the product call.
opensA[0].focus();
if (globalThis.document.activeElement !== opensA[0])
  throw new Error("connected focus must update activeElement");
if (calls.indexOf("open-focus:" + ATT) < 0)
  throw new Error("focus call must be logged");
// Replacement alone — the captured actual markup assigned back —
// disconnects the old set without invoking any renderer: detached
// nodes keep their own state, detached focus is a state no-op,
// and a removed focused node falls back to body.
const snap = box.innerHTML;
box.innerHTML = snap;
if (opensA[0].isConnected !== false || panelA.isConnected !== false)
  throw new Error("replaced nodes must read disconnected");
if (globalThis.document.activeElement !== globalThis.document.body)
  throw new Error("removed focused node falls back to body");
const aeBefore = globalThis.document.activeElement;
opensA[0].focus();
if (globalThis.document.activeElement !== aeBefore)
  throw new Error("detached focus must not move activeElement");
// A stale detached panel cannot leak into the new generation.
panelA.open = false;
const panelB = box.querySelector("details[data-dfix-input-panel]");
if (!panelB || panelB === panelA)
  throw new Error("replacement must build a fresh panel node");
if (panelB.open !== true)
  throw new Error("stale toggle must not alter the current panel");
if (panelA.open !== false)
  throw new Error("old object keeps only its own state");
// Current markup determines the new checked radio identity, and
// the public checked property drives :checked with single
// selection on current nodes.
const radioB = box.querySelector("input[name='prep-dfix-hyp']:checked");
if (!radioB || radioB === radioA || radioB.value !== "H-A")
  throw new Error("checked radio must be a fresh current node");
const allRadios = box.querySelectorAll('input[name="prep-dfix-hyp"]');
const hb = allRadios.filter((r) => r.value === "H-B")[0];
if (!hb) throw new Error("both hypothesis options must render");
hb.checked = true;
if (box.querySelector("input[name='prep-dfix-hyp']:checked") !== hb)
  throw new Error(":checked must follow the public checked property");
if (radioB.checked !== false)
  throw new Error("radio group keeps single selection");
console.log("PASS");
""" % (json.dumps(DERIVED_PREP), json.dumps(derived_payload()),
       json.dumps(viewer_item()), json.dumps(full_input_view())))

