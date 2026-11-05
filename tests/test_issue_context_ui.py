"""Issue-context shipped JS: real extracted dashboard behavior in node.

Drives the actual paintIssueCtx/wireIssueCtxFetch/issueCtx*Html code
against canned /api/run/issue-content payloads. Offline; no network.
Fake DOM is small but faithful: replacing innerHTML detaches the old
subtree (real DOM drops focus to <body>), id lookup, disabled/dataset,
focus tracking, open details. Layout pixels and real caret remain
browser gates.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

HTML = (Path(__file__).resolve().parent.parent / "src" / "openmainty" /
        "dashboard.html").read_text(encoding="utf-8")

def _one_line_func(source, name):
    """Single-line production helpers (esc, bareRunId): exact one match."""
    found = [line for line in source.splitlines()
             if line.startswith("function " + name + "(")]
    if len(found) != 1:
        raise AssertionError(
            "expected exactly one single-line %s, got %d"
            % (name, len(found)))
    return found[0]


def _one_line_const(source, name):
    """Single-line production const: exact one match."""
    found = [line for line in source.splitlines()
             if line.startswith("const " + name + " =")]
    if len(found) != 1:
        raise AssertionError(
            "expected exactly one single-line const %s, got %d"
            % (name, len(found)))
    return found[0]


def _balanced_js(source, prefix):
    """Extract one top-level `function`/`const` starting with prefix.

    Brace counting skips line/block comments and quoted strings so a
    `{` inside a message or regex class cannot end the slice early.
    Template `${...}` pairs stay balanced and count normally.
    """
    start = source.index(prefix)
    i = source.index("{", start)
    depth = 0
    strch = None
    line_comment = block_comment = False
    escaped = False
    n = len(source)
    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if line_comment:
            if ch == "\n":
                line_comment = False
        elif block_comment:
            if ch == "*" and nxt == "/":
                block_comment = False
                i += 1
        elif strch:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == strch:
                strch = None
        elif ch == "/" and nxt == "/":
            line_comment = True
            i += 1
        elif ch == "/" and nxt == "*":
            block_comment = True
            i += 1
        elif ch in ("'", '"', "`"):
            strch = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
        i += 1
    raise AssertionError("unbalanced block for %r" % (prefix,))


def issue_ctx_js():
    """Marker-sliced production block: no brace guessing.

    Slices the contiguous issue-context section between two unique
    markers, then asserts the exact intended function set is present
    and no test-double names leaked in. Copying business logic into
    fixtures is forbidden; this loads the actual shipped code.
    paintIssueCtx renders errors through errBlockHtml, so the driver
    also extracts that helper chain (fetchErrKo, subErrKo and their
    constants) verbatim from the shipped file.
    """
    start = HTML.index("let issueCtxSeq = 0;")
    end = HTML.index("async function renderWorkView(opts) {", start)
    block = HTML[start:end]
    names = set(re.findall(r"^(?:async )?function (\w+)\s*\(", block, re.M))
    assert names == {"issueCtxStateKo", "issueCtxObsHtml",
                     "issueCtxSecHtml", "paintIssueCtx",
                     "wireIssueCtxFetch"}, names
    for banned in ("jget", "jpost", "mkBox", "mkEl", "parseFrag",
                   "renderGlanceView", "renderWorkView"):
        assert not re.search(
            r"^(?:async )?function " + banned + r"\s*\(", block, re.M), \
            banned
    helpers = "\n".join([
        _one_line_func(HTML, "esc"),
        _one_line_func(HTML, "bareRunId"),
        _one_line_const(HTML, "LOGIN_NEEDED_KO"),
        _balanced_js(HTML, "const FETCH_ERR_TEXT ="),
        _one_line_const(HTML, "SUB_APIKEY_RE"),
        _one_line_const(HTML, "SUB_LOGIN_RE"),
        _one_line_const(HTML, "APIKEY_SET_KO"),
        _balanced_js(HTML, "function subErrKo("),
        _balanced_js(HTML, "function fetchErrKo("),
        _balanced_js(HTML, "function errBlockHtml("),
    ])
    return helpers + "\n" + block


PROD_JS = issue_ctx_js()

NODE_PRELUDE = r"""
let GETS = [], POSTS = [];
let GET_MODE = "ok", GET_DELAY = 1, POST_MODE = "ok", POST_DELAY = 1;
let CANNED = null, CANNED_SEQ = [];
let view = {kind: "work", id: "run-x"};
const BODYEL = {tag: "body", id: "", parent: null, children: [], dataset: {}, _root: true};
let ACTIVE = null;
const document = {get activeElement() { return ACTIVE; }, set activeElement(v) { ACTIVE = v; }, body: BODYEL};
function jget(url) {
  GETS.push(url);
  return new Promise((res, rej) => setTimeout(() => {
    if (GET_MODE === "error") return rej(new Error("상세 없음"));
    if (CANNED_SEQ.length) return res(JSON.parse(JSON.stringify(CANNED_SEQ.shift())));
    if (CANNED) return res(JSON.parse(JSON.stringify(CANNED)));
    return rej(new Error("unexpected GET " + url));
  }, GET_DELAY));
}
function jpost(url, body) {
  POSTS.push([url, body]);
  return new Promise((res, rej) => setTimeout(() => {
    if (POST_MODE === "error") return rej(new Error("바쁨"));
    return res(JSON.parse(JSON.stringify(POST_OK)));
  }, POST_DELAY));
}
let POST_OK = {observation: null, changed: true};
function esc2(s) { return String(s ?? ""); }
function resetNet() { GETS = []; POSTS = []; CANNED = null; CANNED_SEQ = []; GET_MODE = "ok"; POST_MODE = "ok"; GET_DELAY = 1; POST_DELAY = 1; POST_OK = {observation: null, changed: true}; view = {kind: "work", id: "run-x"}; ACTIVE = null; issueCtxUI = {}; }
const VOID = {};
function parseAttrs(t) {
  const a = {};
  const re = /([a-zA-Z-]+)="([^"]*)"/g;
  let m;
  while ((m = re.exec(t))) a[m[1]] = m[2];
  return a;
}
function mkText(box, text) {
  return {tag: "#text", text, _box: box, children: [], parent: null,
    dataset: {}, get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); }};
}
function mkEl(box, tag, attrs) {
  const el = {tag, _attrs: attrs, _box: box, children: [], parent: null,
    dataset: {}, id: attrs.id || "", disabled: attrs.disabled !== undefined,
    onclick: null, ontoggle: null,
    get isConnected() { let n = this; while (n) { if (n._root) return true; n = n.parent; } return false; },
    get textContent() {
      const out = [];
      const walk = (n) => { if (n.tag === "#text") out.push(n.text); else (n.children || []).forEach(walk); };
      walk(this);
      return out.join("");
    },
    set textContent(v) {
      const t = mkText(box, String(v)); t.parent = this; this.children = [t];
    },
    get innerHTML() { return this.children.map(serEl).join(""); },
    set innerHTML(v) {
      /* 실제 DOM처럼: 교체로 떨어져 나간 서브트리에 포커스가 있었으면 <body>로.
         떨어진 노드는 연결이 끊긴다(parent=null 재귀). */
      if (ACTIVE && this.contains(ACTIVE)) ACTIVE = BODYEL;
      const detach = (n) => { (n.children || []).forEach(detach); n.parent = null; };
      (this.children || []).forEach(detach);
      this._idcache = null;
      this.children = parseFrag(box, String(v), this);
    },
    querySelector(sel) {
      for (const part of String(sel).split(",")) {
        const s = part.trim();
        if (s.startsWith("#")) { const f = findId(this, s.slice(1)); if (f) return f; }
      }
      return null;
    },
    querySelectorAll() { return []; },
    contains(n) { let c = n; while (c) { if (c === this) return true; c = c.parent; } return false; },
    focus() { ACTIVE = this; },
    click() { if (this.onclick) return this.onclick(); }};
  for (const [k, v] of Object.entries(attrs))
    if (k.startsWith("data-")) el.dataset[k.slice(5)] = v;
  if (tag === "details") { el._open = false; Object.defineProperty(el, "open", {get() { return !!this._open; }, set(v) { const nv = !!v; if (nv !== !!this._open) { this._open = nv; if (this.ontoggle) this.ontoggle(); } }, configurable: true, enumerable: true}); }
  return el;
}
function findId(el, id) {
  if (el.id === id) return el;
  for (const c of (el.children || [])) { const f = findId(c, id); if (f) return f; }
  return null;
}
function serEl(el) {
  if (el.tag === "#text") return el.text;
  const at = Object.entries(el._attrs || {}).map(([k, v]) => ` ${k}="${v}"`).join("");
  return `<${el.tag}${at}>${(el.children || []).map(serEl).join("")}</${el.tag}>`;
}
function parseFrag(box, html, parent) {
  const els = [];
  const stack = [{children: els, el: null}];
  const re = /<\/(h2|div|details|summary|b|a|button|span|p)\s*>|<(h2|div|details|summary|b|a|button|span|p)\b([^>]*?)>|([^<>]+)/g;
  let m;
  while ((m = re.exec(html))) {
    const top = stack[stack.length - 1];
    if (m[4] !== undefined) {
      const t = mkText(box, m[4]); t.parent = top.el;
      top.children.push(t);
    } else if (m[1]) {
      stack.pop();
    } else if (m[2]) {
      const el = mkEl(box, m[2], parseAttrs(m[3] || ""));
      el.parent = top.el;
      top.children.push(el);
      stack.push({children: el.children, el});
    }
  }
  for (const e of els) e.parent = parent || null;
  return els;
}
function mkBox() {
  const box = mkEl(null, "div", {});
  box._root = true;
  return box;
}
function out(name, ok, extra) { console.log((ok ? "PASS " : "FAIL ") + name + (extra ? " :: " + extra : "")); }
"""


def run_node(body):
    with tempfile.TemporaryDirectory() as tmp:
        driver = Path(tmp) / "driver.mjs"
        driver.write_text(NODE_PRELUDE + "\n" + PROD_JS + "\n" + body,
                          encoding="utf-8")
        proc = subprocess.run(["node", str(driver)], capture_output=True,
                              text=True, timeout=60)
    return proc

CASES_JS = r"""
async function main() {
  const OBS = (t, extra) => Object.assign({number: 856, title: t || "T", body: "B", state: "OPEN", url: "https://github.com/o/r/issues/856", provenance: "P", content_fp: "fp-" + (t || "T"), historical_trigger: false}, extra || {});
  const ST_OK = (at, succAt) => ({latest: "issue-856-x", last_attempt: {at: at || "T", ok: true, error: ""}, last_success: {obs: "issue-856-x", at: succAt || at || "T", attempt: "fixed-a"}});

  // 1. pure state labels + escaping
  try {
    const obs = {title: 'T <script>"x"', body: "B & <b>", state: "OPEN", url: "https://github.com/o/r/issues/856?a=1&b=2", provenance: "P"};
    const html = issueCtxObsHtml(obs, "WHEN");
    const hasWhen = html.includes("WHEN");
    const okState = issueCtxStateKo("OPEN") === "열림" && issueCtxStateKo("CLOSED") === "닫힘" && hasWhen;
    out("pure-labels-and-escape", okState && !html.includes("<script>") && html.includes("&lt;script&gt;"), html.slice(0, 80));
  } catch (e) { out("pure-labels-and-escape", false, String(e && e.stack || e)); }

  // 2. never-fetched without recorded trigger: honest "no trigger" copy
  try {
    resetNet();
    CANNED = {applicable: true, cached: false, observation: null, state: {latest: null, last_attempt: null, last_success: null}};
    const box = mkBox();
    await paintIssueCtx(box, {repo: "r", issue: "856", id: "run-x"}, false);
    const t = box.textContent;
    out("never-fetched-no-trigger", t.includes("과거 트리거 원문 없음") && t.includes("현재 GitHub 본문 가져오기") && POSTS.length === 0 && GETS.length === 1, t.slice(0, 140));
  } catch (e) { out("never-fetched-no-trigger", false, String(e && e.stack || e)); }

  // 3. never-fetched WITH recorded trigger: must not claim "no trigger"
  try {
    resetNet();
    CANNED = {applicable: true, cached: false, observation: null, state: {latest: null, last_attempt: null, last_success: null}};
    const box = mkBox();
    await paintIssueCtx(box, {repo: "r", issue: "856", id: "run-x"}, true);
    const t = box.textContent;
    out("never-fetched-with-trigger", t.includes("기록된 시작 원문이 있습니다") && !t.includes("과거 트리거 원문 없음"), t.slice(0, 140));
  } catch (e) { out("never-fetched-with-trigger", false, String(e && e.stack || e)); }

  // 4. cached OPEN observation: success timestamp is last-success, not failure time
  try {
    resetNet();
    CANNED = {applicable: true, cached: true, observation: OBS("Web demo"),
      state: {latest: "issue-856-x", last_attempt: {at: "T-ATT", ok: true, error: ""}, last_success: {obs: "issue-856-x", at: "T-SUCCESS", attempt: "fixed-a"}}};
    const box = mkBox();
    await paintIssueCtx(box, {repo: "r", issue: "856", id: "run-x"}, false);
    const t = box.textContent;
    out("cached-open-renders", t.includes("Web demo") && t.includes("열림") && t.includes("과거 트리거 아님") && t.includes("현재 본문 다시 가져오기") && t.includes("수집 — T-SUCCESS") && t.includes("댓글·라벨·노드 연결은 수집하지 않음"), t.slice(0, 160));
  } catch (e) { out("cached-open-renders", false, String(e && e.stack || e)); }

  // 5. fetch posts once with exact derived identity (no client source fields)
  try {
    resetNet();
    CANNED = {applicable: true, cached: false, observation: null, state: {latest: null, last_attempt: null, last_success: null}};
    const box = mkBox();
    await paintIssueCtx(box, {repo: "r", issue: "856", id: "run-x"}, false);
    const btn = box.querySelector("#issuectx-fetch");
    POST_OK = {observation: OBS("N"), changed: false};
    CANNED = {applicable: true, cached: true, observation: OBS("N"), state: ST_OK("T2", "T2")};
    await btn.click();
    await new Promise(r => setTimeout(r, 30));
    const keys = POSTS.length === 1 ? Object.keys(POSTS[0][1]).sort().join(",") : "";
    const postOk = POSTS.length === 1 && POSTS[0][0] === "/api/run/issue-content/fetch" && keys === "confirm,repo,run" && POSTS[0][1].confirm === true;
    out("fetch-posts-once", postOk, JSON.stringify(POSTS));
  } catch (e) { out("fetch-posts-once", false, String(e && e.stack || e)); }

  // 6. successful fetch repaints with the new observation + success note
  try {
    resetNet();
    const fresh = OBS("Fresh", {state: "CLOSED"});
    CANNED = {applicable: true, cached: false, observation: null, state: {latest: null, last_attempt: null, last_success: null}};
    POST_OK = {observation: fresh, changed: true};
    CANNED = {applicable: true, cached: true, observation: fresh, state: ST_OK("T2", "T2")};
    const box = mkBox();
    await paintIssueCtx(box, {repo: "r", issue: "856", id: "run-x"}, false);
    const btn = box.querySelector("#issuectx-fetch");
    await btn.click();
    await new Promise(r => setTimeout(r, 30));
    const t = box.textContent;
    out("fetch-success-repaints", t.includes("Fresh") && t.includes("닫힘") && t.includes("새 관찰 저장됨"), t.slice(0, 160));
  } catch (e) { out("fetch-success-repaints", false, String(e && e.stack || e)); }

  // 7. fetch failure keeps prior success visible with error, button re-enabled
  try {
    resetNet();
    CANNED = {applicable: true, cached: true, observation: OBS("Old title"),
      state: {latest: "issue-856-x", last_attempt: {at: "T1", ok: true, error: ""}, last_success: {obs: "issue-856-x", at: "T1", attempt: "fixed-a"}}};
    POST_MODE = "error";
    const box = mkBox();
    await paintIssueCtx(box, {repo: "r", issue: "856", id: "run-x"}, false);
    const btn = box.querySelector("#issuectx-fetch");
    await btn.click();
    await new Promise(r => setTimeout(r, 20));
    const t = box.textContent;
    out("fetch-failure-keeps-prior", t.includes("Old title") && t.includes("가져오기 실패") && box._fetching === false, t.slice(0, 200));
  } catch (e) { out("fetch-failure-keeps-prior", false, String(e && e.stack || e)); }

  // 8. failure with no prior success: no "last success" claim
  try {
    resetNet();
    CANNED = {applicable: true, cached: false, observation: null,
      state: {latest: null, last_attempt: {at: "T9", ok: false, error: "boom"}, last_success: null}};
    const box = mkBox();
    await paintIssueCtx(box, {repo: "r", issue: "856", id: "run-x"}, false);
    const t = box.textContent;
    out("failure-without-prior", t.includes("마지막 가져오기 실패") && !t.includes("마지막 성공") && t.includes("현재 GitHub 본문 가져오기"), t.slice(0, 200));
  } catch (e) { out("failure-without-prior", false, String(e && e.stack || e)); }

  // 9. failed attempt does not relabel success time
  try {
    resetNet();
    CANNED = {applicable: true, cached: true, observation: OBS("Keep"),
      state: {latest: "issue-856-x", last_attempt: {at: "T2", ok: false, error: "boom"}, last_success: {obs: "issue-856-x", at: "T-SUCCESS", attempt: "fixed-a"}}};
    const box = mkBox();
    await paintIssueCtx(box, {repo: "r", issue: "856", id: "run-x"}, false);
    const t = box.textContent;
    out("failure-keeps-success-time", t.includes("수집 — T-SUCCESS") && t.includes("마지막 가져오기 실패") && t.includes("T2"), t.slice(0, 220));
  } catch (e) { out("failure-keeps-success-time", false, String(e && e.stack || e)); }

  // 10. late response never paints another run, outsider focus untouched
  try {
    resetNet();
    GET_DELAY = 30;
    CANNED_SEQ = [{applicable: true, cached: true, observation: OBS("RUN-A-TITLE"),
      state: {latest: "x", last_attempt: null, last_success: null}}];
    const box = mkBox();
    const outside = {tag: "div", id: "out", parent: null, children: [], dataset: {}};
    ACTIVE = outside;
    const p = paintIssueCtx(box, {repo: "r", issue: "856", id: "run-a"}, false);
    view = {kind: "work", id: "run-b"};
    await p;
    await new Promise(r => setTimeout(r, 50));
    GET_DELAY = 1;
    const t = box.textContent;
    out("late-response-no-crosspaint", !t.includes("RUN-A-TITLE") && ACTIVE === outside, t.slice(0, 120));
    view = {kind: "work", id: "run-x"};
  } catch (e) { out("late-response-no-crosspaint", false, String(e && e.stack || e)); }

  // 11. non-numeric issue clears box with no GET
  try {
    resetNet();
    const box = mkBox();
    box.innerHTML = "<div>stale</div>";
    await paintIssueCtx(box, {repo: "r", issue: "dep-foo", id: "run-x"}, false);
    out("non-numeric-clears-no-get", box.textContent === "" && GETS.length === 0, box.textContent);
  } catch (e) { out("non-numeric-clears-no-get", false, String(e && e.stack || e)); }

  // 12. GET error offers retry; retry re-issues GET
  try {
    resetNet();
    GET_MODE = "error";
    const box = mkBox();
    await paintIssueCtx(box, {repo: "r", issue: "856", id: "run-x"}, false);
    const n0 = GETS.length;
    const btn = box.querySelector("#issuectx-fetch");
    const hasRetry = !!btn && box.textContent.includes("다시 읽기");
    GET_MODE = "ok";
    CANNED = {applicable: true, cached: false, observation: null, state: {latest: null, last_attempt: null, last_success: null}};
    await btn.click();
    await new Promise(r => setTimeout(r, 20));
    out("get-error-retry", hasRetry && GETS.length > n0, "gets=" + GETS.length);
  } catch (e) { out("get-error-retry", false, String(e && e.stack || e)); }

  // 13. own fetch-button focus survives explicit-fetch repaint
  try {
    resetNet();
    CANNED = {applicable: true, cached: false, observation: null, state: {latest: null, last_attempt: null, last_success: null}};
    POST_OK = {observation: OBS("N"), changed: true};
    CANNED = {applicable: true, cached: true, observation: OBS("N"), state: ST_OK("T3", "T3")};
    const box = mkBox();
    await paintIssueCtx(box, {repo: "r", issue: "856", id: "run-x"}, false);
    const btn = box.querySelector("#issuectx-fetch");
    btn.focus();
    await btn.click();
    await new Promise(r => setTimeout(r, 30));
    const refocused = document.activeElement && document.activeElement.id === "issuectx-fetch" && box.contains(document.activeElement);
    out("fetch-keeps-own-focus", !!refocused, String(document.activeElement && document.activeElement.id));
  } catch (e) { out("fetch-keeps-own-focus", false, String(e && e.stack || e)); }

  // 14. open full-body disclosure survives an async redraw
  try {
    resetNet();
    CANNED = {applicable: true, cached: true, observation: OBS("Web demo"), state: ST_OK("T4", "T4")};
    const box = mkBox();
    await paintIssueCtx(box, {repo: "r", issue: "856", id: "run-x"}, false);
    box.querySelector("#issuectx-body").open = true;
    await paintIssueCtx(box, {repo: "r", issue: "856", id: "run-x"}, false);
    const det = box.querySelector("#issuectx-body");
    out("disclosure-open-preserved", !!(det && det.open === true) && box.textContent.includes("Web demo"), String(det && det.open));
  } catch (e) { out("disclosure-open-preserved", false, String(e && e.stack || e)); }

  // 15. outsider focus is never stolen by a quiet redraw
  try {
    resetNet();
    CANNED = {applicable: true, cached: true, observation: OBS("Q"), state: ST_OK("T5", "T5")};
    const box = mkBox();
    await paintIssueCtx(box, {repo: "r", issue: "856", id: "run-x"}, false);
    const outside = {tag: "input", id: "search", parent: null, children: [], dataset: {}};
    ACTIVE = outside;
    await paintIssueCtx(box, {repo: "r", issue: "856", id: "run-x"}, false);
    out("outsider-focus-untouched", ACTIVE === outside && box.textContent.includes("Q"), String(ACTIVE && ACTIVE.id));
  } catch (e) { out("outsider-focus-untouched", false, String(e && e.stack || e)); }

  // 16. store-error state is explicit, not healthy
  try {
    resetNet();
    CANNED = {applicable: true, cached: false, observation: null, state: {latest: "issue-856-xx", last_attempt: null, last_success: null, store: "error", note: "kept"}};
    const box = mkBox();
    await paintIssueCtx(box, {repo: "r", issue: "856", id: "run-x"}, false);
    out("store-error-explicit", box.textContent.includes("읽을 수 없음") && box.textContent.includes("보존"), box.textContent.slice(0, 120));
  } catch (e) { out("store-error-explicit", false, String(e && e.stack || e)); }

  // 17. closed observation renders closed label, not an error
  try {
    resetNet();
    CANNED = {applicable: true, cached: true, observation: OBS("Old", {body: "", state: "CLOSED"}),
      state: {latest: "issue-856-cc", last_attempt: {at: "T9", ok: true, error: ""}, last_success: {obs: "issue-856-cc", at: "T9", attempt: "fixed-c"}}};
    const box = mkBox();
    await paintIssueCtx(box, {repo: "r", issue: "856", id: "run-x"}, false);
    const t = box.textContent;
    out("closed-renders-closed", t.includes("닫힘") && t.includes("T9") && t.includes("(본문 비어 있음)") && !t.includes("실패"), t.slice(0, 160));
  } catch (e) { out("closed-renders-closed", false, String(e && e.stack || e)); }

  // 19. user moves focus out while POST is pending: never steal back
  try {
    resetNet();
    CANNED = {applicable: true, cached: false, observation: null, state: {latest: null, last_attempt: null, last_success: null}};
    POST_OK = {observation: OBS("After"), changed: true};
    CANNED = {applicable: true, cached: true, observation: OBS("After"), state: ST_OK("T6", "T6")};
    POST_DELAY = 30;
    const box = mkBox();
    await paintIssueCtx(box, {repo: "r", issue: "856", id: "run-x"}, false);
    const btn = box.querySelector("#issuectx-fetch");
    btn.focus();
    const p = btn.click();
    const outside = mkEl(null, "input", {id: "search"}); outside._root = true;
    ACTIVE = outside;
    await p;
    await new Promise(r => setTimeout(r, 60));
    POST_DELAY = 1;
    const t = box.textContent;
    out("no-steal-after-user-moves-out", ACTIVE === outside && t.includes("After"), "active=" + String(ACTIVE && ACTIVE.id));
  } catch (e) { out("no-steal-after-user-moves-out", false, String(e && e.stack || e)); }

  // 20. outer work rebuild restores open body from run memory, not focus
  try {
    resetNet();
    CANNED = {applicable: true, cached: true, observation: OBS("Q"), state: ST_OK("T5", "T5")};
    const box1 = mkBox();
    await paintIssueCtx(box1, {repo: "r", issue: "856", id: "run-x"}, false);
    box1.querySelector("#issuectx-body").open = true;
    const box2 = mkBox();
    await paintIssueCtx(box2, {repo: "r", issue: "856", id: "run-x"}, false);
    const det2 = box2.querySelector("#issuectx-body");
    const kept = !!(det2 && det2.open === true) && box2.textContent.includes("Q");
    view = {kind: "work", id: "run-y"};
    const box3 = mkBox();
    await paintIssueCtx(box3, {repo: "r", issue: "856", id: "run-y"}, false);
    const det3 = box3.querySelector("#issuectx-body");
    view = {kind: "work", id: "run-x"};
    out("outer-rebuild-restores-open", kept && !!det3 && det3.open === false, "box2=" + String(det2 && det2.open) + " box3=" + String(det3 && det3.open));
  } catch (e) { out("outer-rebuild-restores-open", false, String(e && e.stack || e)); }

  // 21. rapid double click issues exactly one POST, flag settles
  try {
    resetNet();
    CANNED = {applicable: true, cached: false, observation: null, state: {latest: null, last_attempt: null, last_success: null}};
    POST_OK = {observation: OBS("D"), changed: true};
    CANNED = {applicable: true, cached: true, observation: OBS("D"), state: ST_OK("T7", "T7")};
    POST_DELAY = 30;
    const box = mkBox();
    await paintIssueCtx(box, {repo: "r", issue: "856", id: "run-x"}, false);
    const btn = box.querySelector("#issuectx-fetch");
    const p1 = btn.click();
    btn.click();
    await p1;
    await new Promise(r => setTimeout(r, 60));
    POST_DELAY = 1;
    out("double-click-single-post", POSTS.length === 1 && box._fetching === false && box.textContent.includes("D"), "posts=" + POSTS.length);
  } catch (e) { out("double-click-single-post", false, String(e && e.stack || e)); }

  // 18. empty body is explicit empty, not an error
  try {
    resetNet();
    CANNED = {applicable: true, cached: true, observation: OBS("T", {body: ""}),
      state: {latest: "issue-856-dd", last_attempt: {at: "T8", ok: true, error: ""}, last_success: {obs: "issue-856-dd", at: "T8", attempt: "fixed-d"}}};
    const box = mkBox();
    await paintIssueCtx(box, {repo: "r", issue: "856", id: "run-x"}, false);
    const t = box.textContent;
    out("empty-body-explicit", t.includes("(본문 비어 있음)") && t.includes("열림"), t.slice(0, 120));
  } catch (e) { out("empty-body-explicit", false, String(e && e.stack || e)); }
}
main().then(() => console.log("DONE"), (e) => { console.log("FATAL " + (e && e.stack || e)); process.exitCode = 1; });
"""

EXPECTED = ["pure-labels-and-escape", "never-fetched-no-trigger",
            "never-fetched-with-trigger", "cached-open-renders",
            "fetch-posts-once", "fetch-success-repaints",
            "fetch-failure-keeps-prior", "failure-without-prior",
            "failure-keeps-success-time", "late-response-no-crosspaint",
            "non-numeric-clears-no-get", "get-error-retry",
            "fetch-keeps-own-focus", "disclosure-open-preserved",
            "outsider-focus-untouched", "store-error-explicit",
            "closed-renders-closed", "empty-body-explicit",
            "no-steal-after-user-moves-out", "outer-rebuild-restores-open",
            "double-click-single-post"]


class TestIssueContextUI(unittest.TestCase):
    def test_shipped_js_behavior(self):
        proc = run_node(CASES_JS)
        self.assertEqual(proc.returncode, 0,
                         msg="node failed:\n" + proc.stderr[-4000:])
        lines = proc.stdout.splitlines()
        self.assertIn("DONE", lines)
        results = {}
        for line in lines:
            m = re.match(r"(PASS|FAIL) (\S+)( :: (.*))?", line)
            if m:
                results[m.group(2)] = (m.group(1), m.group(4) or "")
        for name in EXPECTED:
            self.assertIn(name, results, msg="missing case: " + name)
            status, extra = results[name]
            self.assertEqual(status, "PASS",
                             msg=f"{name} failed: {extra}\n{proc.stdout[-2000:]}")
