"""Decision desk shipped JS: real extracted dashboard functions in node.

Drives the actual deskSectionHtml/deskRowHtml/deskPaneHtml/paintDeskInto/
wireDeskSection/deskSelect/deskRefreshSelected/renderGlanceView code against
canned /api/decisions payloads. Offline; no network. Fake DOM is a small
tree with parent pointers, nested serialization, and query support, so
targeted pane updates, reading-mode classes, and scroll/focus preservation
are observable through the actual render route. Layout metrics, real caret,
and pixels remain real-browser gates.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


NODE_PRELUDE = r"""
const CSS = {escape: (s) => String(s)};
function ago(iso) { return "0분"; }
let GETS = [];
let DESK = null, DETAIL = null, DETAIL_MODE = "ok", DETAIL_DELAY = 0;
let DETAIL_BY_URL = null, DETAIL_DELAY_BY_URL = null;
let SUMMARY = {repos: [], served_at: null, at: null};
let NARROW = false;
const window = {matchMedia: (q) => ({matches: NARROW})};
function jget(url) {
  GETS.push(url);
  let delay = 1;
  if (url.startsWith("/api/decision/detail"))
    delay = ((DETAIL_DELAY_BY_URL && DETAIL_DELAY_BY_URL[url]) || DETAIL_DELAY) || 1;
  return new Promise((res, rej) => setTimeout(() => {
    if (url === "/api/decisions")
      return res(JSON.parse(JSON.stringify(DESK)));
    if (url === "/api/summary")
      return res(JSON.parse(JSON.stringify(SUMMARY)));
    if (url.startsWith("/api/decision/detail")) {
      if (DETAIL_MODE === "error") return rej(new Error("상세 없음"));
      const body = DETAIL_BY_URL ? DETAIL_BY_URL[url] : DETAIL;
      if (body === undefined) return rej(new Error("no fixture for " + url));
      return res(JSON.parse(JSON.stringify(body)));
    }
    return rej(new Error("unexpected GET " + url));
  }, delay));
}
const VOID_TAGS = {input: 1, br: 1, hr: 1, img: 1};
function parseAttrs(attrText) {
  const attrs = {};
  const are = /([a-zA-Z-]+)="([^"]*)"/g;
  let a;
  while ((a = are.exec(attrText))) attrs[a[1]] = a[2];
  attrs.__open = /(?:^|\s)open(?:\s|=|\/|>|$)/.test(attrText);
  return attrs;
}
function mkText(box, text) {
  return {tag: "#text", text, _box: box, _parent: null, children: [],
    dataset: {}, classList: {contains(c) { return false; }}};
}
function mkEl(box, tag, attrs, open) {
  const el = {tag, _attrs: attrs, _open: open, _box: box, _html: "",
    children: [], _parent: null,
    dataset: {}, id: attrs.id || "", open: attrs.__open === true,
    onclick: null, onkeydown: null, oninput: null, ontoggle: null,
    scrollTop: 0,
    setAttribute(k, v) { this._attrs[String(k)] = String(v); },
    removeAttribute(k) { delete this._attrs[String(k)]; },
    getAttribute(k) { const kk = String(k); return kk in this._attrs ? this._attrs[kk] : null; },
    set innerHTML(v) { this._html = String(v);
      this.children = parseFrag(box, this._html, this); },
    get innerHTML() {
      return this.children.length ? this.children.map(serEl).join("") : this._html;
    },
    get textContent() {
      const parts = [];
      const walk = (el) => {
        if (el.tag === "#text") { parts.push(el.text); return; }
        for (const c of (el.children || [])) walk(c);
      };
      walk(this);
      return parts.join("");
    },
    set textContent(v) {
      const t = mkText(box, String(v));
      t._parent = this;
      this.children = [t];
    },
    insertAdjacentHTML(pos, html) {
      if (pos !== "beforeend") throw new Error("unsupported insertAdjacentHTML position " + pos);
      for (const k of parseFrag(box, String(html), this)) { k._parent = this; this.children.push(k); }
    },
    querySelector(sel) { const all = searchEls(this.children, sel, []); return all.length ? all[0] : null; },
    querySelectorAll(sel) { return searchEls(this.children, sel, []); },
    click() { if (this.onclick) return this.onclick(); },
    focus() { document.activeElement = this; box._focused = this; },
    remove() {
      const p = this._parent;
      if (p && p.children) {
        const i = p.children.indexOf(this);
        if (i >= 0) p.children.splice(i, 1);
      }
      this._parent = null;
    }};
  for (const [k, v] of Object.entries(attrs)) {
    if (k.startsWith("data-")) el.dataset[k.slice(5)] = v;
  }
  el.classList = {_s: new Set((attrs.class || "").split(/\s+/).filter(Boolean)),
    add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
    contains(c) { return this._s.has(c); }};
  return el;
}
function serEl(el) {
  if (!el) return "";
  if (el.tag === "#text") return el.text;
  const inner = (el.children || []).map(serEl).join("");
  if (VOID_TAGS[el.tag]) return el._open;
  return el._open + inner + "</" + el.tag + ">";
}
function parseFrag(box, html, parent) {
  const els = [];
  const stack = [{children: els, el: null}];
  const re = /<\/(button|details|div|h1|h2|h3|a|summary|span|p|ul|li|i|b|input|br)\s*>|<(button|details|div|h1|h2|h3|a|summary|span|p|ul|li|i|b|input|br)\b([^>]*?)(\/?)>|([^<>]+)/g;
  let m;
  while ((m = re.exec(html))) {
    const top = stack[stack.length - 1];
    if (m[5] !== undefined) {
      const t = mkText(box, m[5]);
      t._parent = top.el;
      top.children.push(t);
    } else if (m[2]) {
      const el = mkEl(box, m[2], parseAttrs(m[3] || ""), m[0].replace(/\/?>$/, ">"));
      el._parent = top.el;
      top.children.push(el);
      if (!m[4] && !VOID_TAGS[m[2]]) stack.push({children: el.children, el});
    } else if (m[1]) {
      if (stack.length > 1) stack.pop();
    }
  }
  return els;
}
function matchSel(el, sel) {
  if (!el || el.tag === "#text") return false;
  let rest = sel;
  const tm = rest.match(/^([a-z]+)(.*)$/);
  let tag = null;
  if (tm && (tm[2].startsWith("#") || tm[2].startsWith("[") || tm[2].startsWith("."))) {
    tag = tm[1]; rest = tm[2];
  } else if (tm && /^[a-z]+$/.test(rest)) {
    return el.tag === rest;
  }
  if (rest.startsWith("#")) {
    const id = rest.slice(1);
    return (!tag || el.tag === tag) && el.id === id;
  }
  let mo = rest.match(/^\.([a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+)+)$/);
  if (mo) return mo[1].split(".").every(c => el.classList && el.classList.contains(c));
  if (rest.startsWith(".")) {
    const c = rest.slice(1);
    return (!tag || el.tag === tag) && el.classList && el.classList.contains(c);
  }
  mo = rest.match(/^\[data-([a-z-]+)="([^"]*)"\]\[open\]$/);
  if (mo) return (!tag || el.tag === tag) && el.dataset[mo[1]] === mo[2] && el.open;
  mo = rest.match(/^\[data-([a-z-]+)\]\[open\]$/);
  if (mo) return (!tag || el.tag === tag) && el.dataset[mo[1]] !== undefined && el.open;
  mo = rest.match(/^\[data-([a-z-]+)="([^"]*)"\]$/);
  if (mo) return (!tag || el.tag === tag) && el.dataset[mo[1]] === mo[2];
  mo = rest.match(/^\[data-([a-z-]+)\]$/);
  if (mo) return (!tag || el.tag === tag) && el.dataset[mo[1]] !== undefined;
  return false;
}
function searchEls(els, sel, out) {
  for (const el of els) {
    if (matchSel(el, sel)) out.push(el);
    if (el.children && el.children.length) searchEls(el.children, sel, out);
  }
  return out;
}
function mkBox() {
  const box = {_html: "", _desk: undefined, _paneKey: undefined, _changedId: null,
    _focused: null, children: [], dataset: {}, _attrs: {},
    setAttribute(k, v) { this._attrs[String(k)] = String(v); },
    removeAttribute(k) { delete this._attrs[String(k)]; },
    getAttribute(k) { const kk = String(k); return kk in this._attrs ? this._attrs[kk] : null; },
    set innerHTML(v) { this._html = String(v); this.children = parseFrag(this, this._html, null); },
    get innerHTML() {
      return this.children.length ? this.children.map(serEl).join("") : this._html;
    },
    querySelectorAll(sel) { return searchEls(this.children, sel, []); },
    querySelector(sel) { const all = this.querySelectorAll(sel); return all.length ? all[0] : null; }};
  return box;
}
const deskBox = mkBox();
const extraRoots = [];
const document = {activeElement: null,
  querySelector(sel) {
    const direct = deskBox.querySelector(sel);
    if (direct) return direct;
    for (const r of extraRoots) { const hit = r.querySelector(sel); if (hit) return hit; }
    return null;
  },
  querySelectorAll(sel) {
    let out = deskBox.querySelectorAll(sel);
    for (const r of extraRoots) out = out.concat(r.querySelectorAll(sel));
    return out;
  }};
"""


def desk_js():
    src = Path(__file__).resolve().parent.parent / "src" / "openmainty" / "dashboard.html"
    html = src.read_text(encoding="utf-8")
    esc_line = next(line for line in html.splitlines()
                    if line.startswith("function esc("))
    start = html.index("/* Decision Desk")
    end = html.index("async function renderGlanceView(", start)
    return esc_line + "\n" + html[start:end]


def err_js():
    src = Path(__file__).resolve().parent.parent / "src" / "openmainty" / "dashboard.html"
    html = src.read_text(encoding="utf-8")
    start = html.index("const FETCH_ERR_TEXT = {")
    end = html.index("function glanceRepoCard(r) {")
    return html[start:end]


def glance_js():
    src = Path(__file__).resolve().parent.parent / "src" / "openmainty" / "dashboard.html"
    html = src.read_text(encoding="utf-8")
    # The glance band helpers live just before renderGlanceView; the band
    # under test must run through the actual verdict/sentence/html code.
    band_start = html.index("/* 한눈에 보기 맨 위 상태 띠(순수)")
    band_end = html.index("function glanceRepoCard(r) {")
    render_start = html.index("async function renderGlanceView(")
    end = html.index("async function renderDigestView(", render_start)
    consts = "\n".join(
        line for line in html.splitlines()
        if line.startswith("const M1_VERDICT_KO")
        or line.startswith("const M1_VERDICT_CLS"))
    if not consts:
        raise AssertionError("missing M1 verdict maps for the band sandbox")
    return consts + "\n" + html[band_start:band_end] + "\n" + html[render_start:end]


GLANCE_STUBS = r"""
const viewBox = mkBox();
var O = {repos: []};
extraRoots.push(viewBox);
function $(id) {
  if (id === "view") return viewBox;
  if (id === "bartitle") return {set textContent(v) {}};
  return null;
}
let PARSE_KIND = "glance";
function parseHash() { return {kind: PARSE_KIND}; }
function glanceSortRows(repos) { return repos || []; }
function glanceRepoCard() { return ""; }
function glossaryDetails() { return ""; }
function glanceOpenKeys() { return []; }
function glanceScrollTop() { return 0; }
function restoreGlanceOpen() {}
function restoreGlanceScroll() {}
function focusViewHeading() { return false; }
"""


def run_driver(driver_js, setup_js="", extra_js=""):
    import subprocess
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as fh:
        # err_js() supplies the shipped error-copy helpers for desk panes that
        # render them. Suites that already inline glance_js() carry the same
        # block, so only append it when it is missing (duplicate const
        # declarations would fail the whole file, not the product).
        err_block = "" if "function errBlockHtml(" in extra_js else err_js() + "\n"
        fh.write(NODE_PRELUDE + desk_js() + "\n" + err_block + extra_js + "\n"
                 + setup_js + driver_js)
        name = fh.name
    try:
        proc = subprocess.run(["node", name], capture_output=True,
                              text=True, timeout=120)
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass
    if proc.returncode != 0:
        raise AssertionError(f"node failed: {proc.stderr[:2000]}")
    return [json.loads(line) for line in proc.stdout.splitlines()
            if line.strip()]


def desk_payload():
    return {
        "unknown": False, "error": None, "revision": "2026-10-27T00:00:00+00:00",
        "repos": [{"name": "cal",
                   "counts": {"need_you_total": 2, "need_you_supported": 1,
                              "blocked_total": 1}}],
        "decisions": [],
        "supported": [{
            "id": "cal:question:run-1", "repo": "cal", "kind": "question",
            "ref": "run-1", "title_excerpt": "#431 Foo?", "title_is_excerpt": True,
            "age_days": 9, "age_text": "9일째 대기", "why": "답이 없으면 멈춤",
            "suggested": "버전을 확인하세요", "suggested_basis": "규칙 R-3",
            "supported": True,
            "revision": "2026-10-27T00:00:00+00:00"}],
        "unsupported": [{
            "id": "cal:prepare-retry:run-9", "repo": "cal", "kind": "prepare-retry",
            "ref": "run-9", "title_excerpt": "retry", "title_is_excerpt": True,
            "age_days": 1, "age_text": "1일째", "why": "", "suggested": "",
            "suggested_basis": "", "supported": False,
            "support_note": "지원하지 않음",
            "revision": "2026-10-27T00:00:00+00:00"}]}


def detail_payload(question="full question text here"):
    return {
        "unknown": False, "error": None, "inactive": False,
        "id": "cal:question:run-1", "repo": "cal", "kind": "question",
        "ref": "run-1", "question_full": question, "question_pending": True,
        "issue": "431", "title": "Migrate",
        "inputs": {"version": 1, "repo": "cal", "kind": "question", "ref": "run-1",
                   "wait_reason": "question: " + question,
                   "work_tree_fingerprint": "f" * 64,
                   "base_commit": "b" * 40, "work_commit": "c" * 40,
                   "steps": {"triage": "blocked"}},
        "steps": {"triage": "blocked"},
        "issue_url": "https://github.com/o/cal/issues/431"}


def mount_root():
    return """
deskBox.innerHTML = '<div id="deskroot"></div>';
const root = deskBox.querySelector("#deskroot");
root._desk = DESK;
paintDeskInto(root, false);
"""


class DeskRenderTest(unittest.TestCase):
    def test_section_lists_supported_counts_and_unsupported(self):
        setup = "DESK = %s;" % json.dumps(desk_payload())
        out = run_driver("""
(async () => {
""" + mount_root() + """
  const html = deskBox.innerHTML;
  console.log(JSON.stringify({
    head: html.includes("오늘 결정할 일"),
    rail: html.includes("다른 종류 1건") && html.includes("막힘 1건"),
    row: html.includes("#431 Foo?") && html.includes("질문"),
    uns: html.includes("다른 종류 1건"),
    unsAction: html.includes("전체 목록에서 확인"),
    sel: html.includes('aria-current="false"'),
    guide: html.includes("목록에서 판단을 선택하세요")
  }));
})();
""", setup)
        for key in ("head", "rail", "row", "uns", "unsAction", "sel",
                    "guide"):
            self.assertTrue(out[0][key], key)

    def test_selected_pane_shows_recorded_provenance_not_model(self):
        setup = ("DESK = %s; DETAIL = %s;" % (
            json.dumps(desk_payload()), json.dumps(detail_payload())))
        out = run_driver("""
(async () => {
""" + mount_root() + """
  await deskBox.querySelector("[data-deskrow]").click();
  await new Promise(r => setTimeout(r, 30));
  const pane = deskBox.querySelector("[data-deskpane]").innerHTML;
  console.log(JSON.stringify({
    recorded: pane.includes("기록된 제안") && pane.includes("버전을 확인하세요"),
    basis: pane.includes("규칙 R-3"),
    noModel: pane.indexOf("Muse 제안") < 0,
    title: pane.includes("<h3") && pane.includes("Migrate"),
    fullQ: pane.includes("질문 전문") && pane.includes("full question text here")
  }));
})();
""", setup)
        for key in ("recorded", "basis", "noModel", "title", "fullQ"):
            self.assertTrue(out[0][key], key)

    def test_unknown_snapshot_is_typed_error_with_retry(self):
        setup = "DESK = %s;" % json.dumps({
            "unknown": True, "error": "snapshot boom", "repos": [],
            "decisions": [], "supported": [], "unsupported": [],
            "revision": None})
        out = run_driver("""
(async () => {
""" + mount_root() + """
  const html = deskBox.innerHTML;
  const btns = deskBox.querySelectorAll("[data-deskreload]");
  console.log(JSON.stringify({
    err: html.includes("snapshot boom"),
    retry: btns.length === 1,
    noRow: deskBox.querySelectorAll("[data-deskrow]").length === 0
  }));
})();
""", setup)
        for key in ("err", "retry", "noRow"):
            self.assertTrue(out[0][key], key)

    def test_malformed_inputs_never_compare_current(self):
        setup = "DESK = %s;" % json.dumps(desk_payload())
        out = run_driver("""
(async () => {
  const good = %s;
  const kGood = deskInputsKey(good);
  const badA = JSON.parse(JSON.stringify(good));
  delete badA.inputs.kind;
  const kA = deskInputsKey(badA);
  const badB = JSON.parse(JSON.stringify(good));
  badB.inputs.version = true;
  const kB = deskInputsKey(badB);
  console.log(JSON.stringify({
    goodValid: kGood.indexOf("v1|") === 0,
    aInvalid: kA.indexOf("invalid:") === 0,
    bInvalid: kB.indexOf("invalid:") === 0,
    distinct: kA !== kB && kA !== kGood && kB !== kGood
  }));
})();
""" % json.dumps(detail_payload()), setup)
        for key in ("goodValid", "aInvalid", "bInvalid", "distinct"):
            self.assertTrue(out[0][key], key)

    def test_fingerprint_reflects_content_key(self):
        setup = "DESK = %s;" % json.dumps(desk_payload())
        out = run_driver("""
(async () => {
  const det = %s;
  const k1 = deskInputsKey(det);
  const changed = JSON.parse(JSON.stringify(det));
  changed.inputs.wait_reason = "question: edited";
  const k2 = deskInputsKey(changed);
  const fp1 = deskFp(DESK, "cal:question:run-1", k1);
  const fp2 = deskFp(DESK, "cal:question:run-1", k2);
  console.log(JSON.stringify({diff: k1 !== k2, fpDiff: fp1 !== fp2}));
})();
""" % json.dumps(detail_payload()), setup)
        self.assertTrue(out[0]["diff"])
        self.assertTrue(out[0]["fpDiff"])


class DeskFlowTest(unittest.TestCase):
    def test_select_shows_evidence_and_moves_focus_once(self):
        setup = ("DESK = %s; DETAIL = %s;" % (
            json.dumps(desk_payload()), json.dumps(detail_payload())))
        out = run_driver("""
(async () => {
""" + mount_root() + """
  const rows = deskBox.querySelectorAll("[data-deskrow]");
  if (rows.length !== 1) throw new Error("want 1 row");
  await rows[0].click();
  await new Promise(r => setTimeout(r, 30));
  const html = deskBox.innerHTML;
  const ae = document.activeElement;
  console.log(JSON.stringify({
    gets: GETS.filter(u => u.startsWith("/api/decision/detail")).length,
    full: html.includes("full question text here"),
    issue: html.includes("https://github.com/o/cal/issues/431"),
    fp: html.includes("ffffffffffff"),
    answer: html.includes("실행 기록에서 답하기"),
    head: html.includes('data-deskhead="cal:question:run-1"'),
    focusMoved: !!(ae && ae.dataset && ae.dataset.deskhead === "cal:question:run-1")
  }));
})();
""", setup)
        self.assertEqual(out[0]["gets"], 1)
        for key in ("full", "issue", "fp", "answer", "head", "focusMoved"):
            self.assertTrue(out[0][key], key)

    def test_issue_review_uses_review_action_not_answer(self):
        payload = desk_payload()
        payload["supported"].append({
            "id": "cal:issue-review:run-2", "repo": "cal",
            "kind": "issue-review", "ref": "run-2",
            "title_excerpt": "#737 crash?", "title_is_excerpt": True,
            "age_days": 9, "age_text": "9일째 대기", "why": "검토 필요",
            "suggested": "", "suggested_basis": "", "supported": True,
            "revision": "2026-10-27T00:00:00+00:00"})
        det = detail_payload()
        det.update({"id": "cal:issue-review:run-2", "kind": "issue-review",
                    "ref": "run-2", "question_full": "",
                    "question_pending": False, "title": "Crash report",
                    "inputs": dict(det["inputs"], kind="issue-review",
                                   ref="run-2", wait_reason="")})
        setup = ("DESK = %s; DETAIL = %s;" % (
            json.dumps(payload), json.dumps(det)))
        out = run_driver("""
(async () => {
""" + mount_root() + """
  const rows = deskBox.querySelectorAll('[data-deskrow="cal:issue-review:run-2"]');
  if (rows.length !== 1) throw new Error("want review row");
  await rows[0].click();
  await new Promise(r => setTimeout(r, 30));
  const html = deskBox.innerHTML;
  console.log(JSON.stringify({
    view: html.includes("실행 기록 보기"),
    noAnswer: !html.includes("실행 기록에서 답하기"),
    noPendingWarn: !html.includes("더 이상 답변을 기다리지 않습니다"),
    kindNote: html.includes("질문이 아니라 검토 대상")
  }));
})();
""", setup)
        for key in ("view", "noAnswer", "noPendingWarn", "kindNote"):
            self.assertTrue(out[0][key], key)

    def test_back_clears_selection_and_returns_focus(self):
        setup = ("DESK = %s; DETAIL = %s;" % (
            json.dumps(desk_payload()), json.dumps(detail_payload())))
        out = run_driver("""
(async () => {
""" + mount_root() + """
  await deskBox.querySelector("[data-deskrow]").click();
  await new Promise(r => setTimeout(r, 30));
  const backs = deskBox.querySelectorAll("[data-deskback]");
  if (!backs.length) throw new Error("no back button");
  await backs[0].click();
  await new Promise(r => setTimeout(r, 20));
  const html = deskBox.innerHTML;
  const ae = document.activeElement;
  console.log(JSON.stringify({
    guide: html.includes("목록에서 판단을 선택하세요"),
    focusRow: !!(ae && ae.dataset && ae.dataset.deskrow === "cal:question:run-1"),
    unselected: html.includes('aria-current="false"')
  }));
})();
""", setup)
        for key in ("guide", "focusRow", "unselected"):
            self.assertTrue(out[0][key], key)

    def test_quiet_poll_preserves_pane_on_same_key(self):
        setup = ("DESK = %s; DETAIL = %s;" % (
            json.dumps(desk_payload()), json.dumps(detail_payload())))
        out = run_driver("""
(async () => {
""" + mount_root() + """
  await deskBox.querySelector("[data-deskrow]").click();
  await new Promise(r => setTimeout(r, 30));
  const paneBefore = deskBox.querySelector("[data-deskpane]").innerHTML;
  // Quiet poll: same decisions, only volatile age text changed.
  root._desk = JSON.parse(JSON.stringify(DESK));
  root._desk.supported[0].age_text = "10일째 대기";
  paintDeskInto(root, true);
  const paneAfter = deskBox.querySelector("[data-deskpane]").innerHTML;
  const listHtml = deskBox.querySelector("[data-desklist]").innerHTML;
  console.log(JSON.stringify({
    paneKept: paneBefore === paneAfter,
    listFresh: listHtml.includes("10일째 대기")
  }));
})();
""", setup)
        self.assertTrue(out[0]["paneKept"])
        self.assertTrue(out[0]["listFresh"])

    def test_changed_content_repaints_with_badge(self):
        setup = ("DESK = %s; DETAIL = %s;" % (
            json.dumps(desk_payload()), json.dumps(detail_payload())))
        out = run_driver("""
(async () => {
""" + mount_root() + """
  await deskBox.querySelector("[data-deskrow]").click();
  await new Promise(r => setTimeout(r, 30));
  // New snapshot revision with identical decisions.
  const desk2 = JSON.parse(JSON.stringify(DESK));
  desk2.revision = "2026-10-27T01:00:00+00:00";
  root._desk = desk2;
  // Background refresh finds edited content.
  const edited = JSON.parse(JSON.stringify(DETAIL));
  edited.inputs.wait_reason = "question: edited text";
  edited.question_full = "edited text";
  DETAIL = edited;
  await deskRefreshSelected(root);
  const html = deskBox.innerHTML;
  console.log(JSON.stringify({
    gets: GETS.filter(u => u.startsWith("/api/decision/detail")).length,
    badge: html.includes("바뀐 근거가 반영됐습니다"),
    content: html.includes("edited text")
  }));
})();
""", setup)
        self.assertEqual(out[0]["gets"], 2)
        self.assertTrue(out[0]["badge"])
        self.assertTrue(out[0]["content"])

    def test_stale_cache_refetch_with_identical_content_cannot_stick_loading(self):
        setup = ("DESK = %s; DETAIL = %s;" % (
            json.dumps(desk_payload()), json.dumps(detail_payload())))
        out = run_driver("""
(async () => {
""" + mount_root() + """
  await deskBox.querySelector("[data-deskrow]").click();
  await new Promise(r => setTimeout(r, 30));
  // Snapshot moved on, but the stored content is byte-identical.
  const desk2 = JSON.parse(JSON.stringify(DESK));
  desk2.revision = "2026-10-27T01:00:00+00:00";
  DESK = desk2;
  deskRev = "2026-10-27T01:00:00+00:00";
  await deskBox.querySelector("[data-deskrow]").click();
  await new Promise(r => setTimeout(r, 30));
  const pane = deskBox.querySelector("[data-deskpane]").innerHTML;
  console.log(JSON.stringify({
    gets: GETS.filter(u => u.startsWith("/api/decision/detail")).length,
    detail: pane.includes("full question text here"),
    noLoading: pane.indexOf("읽는 중") < 0,
    noBadge: pane.indexOf("바뀐 근거가 반영됐습니다") < 0
  }));
})();
""", setup)
        self.assertEqual(out[0]["gets"], 2)
        for key in ("detail", "noLoading", "noBadge"):
            self.assertTrue(out[0][key], key)

    def test_unchanged_refresh_keeps_pane_without_repaint(self):
        setup = ("DESK = %s; DETAIL = %s;" % (
            json.dumps(desk_payload()), json.dumps(detail_payload())))
        out = run_driver("""
(async () => {
""" + mount_root() + """
  await deskBox.querySelector("[data-deskrow]").click();
  await new Promise(r => setTimeout(r, 30));
  const paneBefore = deskBox.querySelector("[data-deskpane]").innerHTML;
  const desk2 = JSON.parse(JSON.stringify(DESK));
  desk2.revision = "2026-10-27T01:00:00+00:00";
  root._desk = desk2;
  await deskRefreshSelected(root);
  const paneAfter = deskBox.querySelector("[data-deskpane]").innerHTML;
  console.log(JSON.stringify({
    gets: GETS.filter(u => u.startsWith("/api/decision/detail")).length,
    kept: paneBefore === paneAfter,
    noBadge: deskBox.innerHTML.indexOf("바뀐 근거가 반영됐습니다") < 0
  }));
})();
""", setup)
        self.assertEqual(out[0]["gets"], 2)
        self.assertTrue(out[0]["kept"])
        self.assertTrue(out[0]["noBadge"])

    def test_newer_selection_wins_over_stale_response(self):
        payload = desk_payload()
        payload["supported"].append({
            "id": "cal:question:run-2", "repo": "cal", "kind": "question",
            "ref": "run-2", "title_excerpt": "Second?", "title_is_excerpt": True,
            "age_days": 1, "age_text": "1일째 대기", "why": "", "suggested": "",
            "suggested_basis": "", "supported": True,
            "revision": "2026-10-27T00:00:00+00:00"})
        body2 = detail_payload(question="Second full text")
        body2.update({"id": "cal:question:run-2", "ref": "run-2",
                      "title": "Second title",
                      "inputs": dict(body2["inputs"], ref="run-2",
                                     wait_reason="question: Second full text")})
        by_url = {
            "/api/decision/detail?repo=cal&id=cal%3Aquestion%3Arun-1":
                detail_payload(),
            "/api/decision/detail?repo=cal&id=cal%3Aquestion%3Arun-2": body2,
        }
        setup = ("DESK = %s; DETAIL_BY_URL = %s;\n"
                 "DETAIL_DELAY_BY_URL = %s;" % (
                     json.dumps(payload), json.dumps(by_url), json.dumps({
                         "/api/decision/detail?repo=cal&id=cal%3Aquestion%3Arun-1": 60,
                         "/api/decision/detail?repo=cal&id=cal%3Aquestion%3Arun-2": 5})))
        out = run_driver("""
(async () => {
""" + mount_root() + """
  const rows = deskBox.querySelectorAll("[data-deskrow]");
  const p1 = rows[0].click();
  const p2 = rows[1].click();
  await Promise.all([p1, p2]);
  await new Promise(r => setTimeout(r, 30));
  const html = deskBox.innerHTML;
  const ae = document.activeElement;
  console.log(JSON.stringify({
    paneSecond: html.includes("Second full text"),
    noFirst: html.indexOf("full question text here") < 0,
    headSecond: html.includes('data-deskhead="cal:question:run-2"'),
    focusSecond: !!(ae && ae.dataset && ae.dataset.deskhead === "cal:question:run-2")
  }));
})();
""", setup)
        # run-1 is slow (60ms), run-2 is fast (5ms): the late run-1
        # response must not repaint the run-2 pane.
        for key in ("paneSecond", "noFirst", "headSecond", "focusSecond"):
            self.assertTrue(out[0][key], key)

    def test_failed_detail_shows_retry_and_recovers(self):
        setup = 'DESK = %s; DETAIL_MODE = "error";' % json.dumps(desk_payload())
        out = run_driver("""
(async () => {
""" + mount_root() + """
  await deskBox.querySelector("[data-deskrow]").click();
  await new Promise(r => setTimeout(r, 30));
  let html = deskBox.innerHTML;
  const retry = html.includes("다시 불러오기");
  DETAIL_MODE = "ok";
  DETAIL = %s;
  const rb = deskBox.querySelectorAll("[data-deskretry]");
  if (rb.length) { await rb[0].click(); await new Promise(r => setTimeout(r, 40)); }
  html = deskBox.innerHTML;
  console.log(JSON.stringify({retry,
    recovered: html.includes("full question text here")}));
})();
""" % json.dumps(detail_payload()), setup)
        self.assertTrue(out[0]["retry"])
        self.assertTrue(out[0]["recovered"])

    def test_failed_retry_repaints_error_without_suppression(self):
        setup = 'DESK = %s; DETAIL_MODE = "error";' % json.dumps(desk_payload())
        out = run_driver("""
(async () => {
""" + mount_root() + """
  await deskBox.querySelector("[data-deskrow]").click();
  await new Promise(r => setTimeout(r, 30));
  const first = deskBox.querySelector("[data-deskpane]").innerHTML;
  // Retry while the backend still fails: the error screen must
  // repaint (not be swallowed by the render key), with a way back.
  const rb = deskBox.querySelectorAll("[data-deskretry]");
  if (!rb.length) throw new Error("no retry button");
  await rb[0].click();
  await new Promise(r => setTimeout(r, 40));
  const second = deskBox.querySelector("[data-deskpane]").innerHTML;
  console.log(JSON.stringify({
    stillError: second.includes("상세 없음"),
    hasBack: deskBox.querySelectorAll("[data-deskback]").length >= 1,
    gets: GETS.filter(u => u.startsWith("/api/decision/detail")).length
  }));
})();
""", setup)
        self.assertTrue(out[0]["stillError"])
        self.assertTrue(out[0]["hasBack"])
        self.assertEqual(out[0]["gets"], 2)

    def test_same_row_reselect_keeps_cached_detail(self):
        setup = ("DESK = %s; DETAIL = %s;" % (
            json.dumps(desk_payload()), json.dumps(detail_payload())))
        out = run_driver("""
(async () => {
""" + mount_root() + """
  const row = deskBox.querySelector("[data-deskrow]");
  await row.click();
  await new Promise(r => setTimeout(r, 30));
  // Re-click the same cached row: no fetch, no stuck loading screen.
  await deskBox.querySelector("[data-deskrow]").click();
  await new Promise(r => setTimeout(r, 20));
  const pane = deskBox.querySelector("[data-deskpane]").innerHTML;
  console.log(JSON.stringify({
    gets: GETS.filter(u => u.startsWith("/api/decision/detail")).length,
    detail: pane.includes("full question text here"),
    noLoading: pane.indexOf("읽는 중") < 0
  }));
})();
""", setup)
        self.assertEqual(out[0]["gets"], 1)
        self.assertTrue(out[0]["detail"])
        self.assertTrue(out[0]["noLoading"])

    def test_cached_back_reopen_uses_cache_without_refetch(self):
        setup = ("DESK = %s; DETAIL = %s;" % (
            json.dumps(desk_payload()), json.dumps(detail_payload())))
        out = run_driver("""
(async () => {
""" + mount_root() + """
  await deskBox.querySelector("[data-deskrow]").click();
  await new Promise(r => setTimeout(r, 30));
  await deskBox.querySelector("[data-deskback]").click();
  await new Promise(r => setTimeout(r, 20));
  await deskBox.querySelector("[data-deskrow]").click();
  await new Promise(r => setTimeout(r, 20));
  const pane = deskBox.querySelector("[data-deskpane]").innerHTML;
  console.log(JSON.stringify({
    gets: GETS.filter(u => u.startsWith("/api/decision/detail")).length,
    detail: pane.includes("full question text here")
  }));
})();
""", setup)
        self.assertEqual(out[0]["gets"], 1)
        self.assertTrue(out[0]["detail"])

    def test_failed_background_refresh_keeps_content_with_retry(self):
        setup = ("DESK = %s; DETAIL = %s;" % (
            json.dumps(desk_payload()), json.dumps(detail_payload())))
        out = run_driver("""
(async () => {
""" + mount_root() + """
  await deskBox.querySelector("[data-deskrow]").click();
  await new Promise(r => setTimeout(r, 30));
  const desk2 = JSON.parse(JSON.stringify(DESK));
  desk2.revision = "2026-10-27T01:00:00+00:00";
  root._desk = desk2;
  DETAIL_MODE = "error";
  await deskRefreshSelected(root);
  const pane = deskBox.querySelector("[data-deskpane]").innerHTML;
  console.log(JSON.stringify({
    kept: pane.includes("full question text here"),
    note: pane.includes("최신 읽기 실패"),
    retry: deskBox.querySelectorAll("[data-deskretry]").length >= 1,
    noBadge: pane.indexOf("바뀐 근거가 반영됐습니다") < 0
  }));
})();
""", setup)
        for key in ("kept", "note", "retry", "noBadge"):
            self.assertTrue(out[0][key], key)

    def test_removed_membership_shows_inactive_without_losing_cache(self):
        setup = ("DESK = %s; DETAIL = %s;" % (
            json.dumps(desk_payload()), json.dumps(detail_payload())))
        out = run_driver("""
(async () => {
""" + mount_root() + """
  await deskBox.querySelector("[data-deskrow]").click();
  await new Promise(r => setTimeout(r, 30));
  // New summary no longer lists the selected decision.
  const desk2 = JSON.parse(JSON.stringify(DESK));
  desk2.revision = "2026-10-27T01:00:00+00:00";
  desk2.supported = [];
  root._desk = desk2;
  paintDeskInto(root, true);
  const pane = deskBox.querySelector("[data-deskpane]").innerHTML;
  console.log(JSON.stringify({
    inactive: pane.includes("최신 요약에 없는 항목"),
    back: deskBox.querySelectorAll("[data-deskback]").length >= 1,
    cacheKept: !!(typeof deskCache !== "undefined" && deskCache["cal:question:run-1"])
  }));
})();
""", setup)
        for key in ("inactive", "back", "cacheKept"):
            self.assertTrue(out[0][key], key)

    def test_quiet_poll_does_not_steal_focus(self):
        setup = ("DESK = %s; DETAIL = %s;" % (
            json.dumps(desk_payload()), json.dumps(detail_payload())))
        out = run_driver("""
(async () => {
""" + mount_root() + """
  document.activeElement = {dataset: {elsewhere: "1"}};
  const before = document.activeElement;
  paintDeskInto(root, true);
  console.log(JSON.stringify({kept: document.activeElement === before}));
})();
""", setup)
        self.assertTrue(out[0]["kept"])

    def test_narrow_reading_mode_and_back_restore(self):
        setup = ("DESK = %s; DETAIL = %s; NARROW = true;" % (
            json.dumps(desk_payload()), json.dumps(detail_payload())))
        out = run_driver("""
(async () => {
""" + mount_root() + """
  const list = deskBox.querySelector("[data-desklist]");
  list.scrollTop = 123;
  await deskBox.querySelector("[data-deskrow]").click();
  await new Promise(r => setTimeout(r, 30));
  const grid = deskBox.querySelector(".dd-grid");
  const reading = !!(grid && grid.classList.contains("dd-reading"));
  await deskBox.querySelector("[data-deskback]").click();
  await new Promise(r => setTimeout(r, 20));
  const gridAfter = deskBox.querySelector(".dd-grid");
  console.log(JSON.stringify({
    reading,
    listBack: !(gridAfter && gridAfter.classList.contains("dd-reading")),
    scroll: deskBox.querySelector("[data-desklist]").scrollTop === 123
  }));
})();
""", setup)
        self.assertTrue(out[0]["reading"])
        self.assertTrue(out[0]["listBack"])
        self.assertTrue(out[0]["scroll"])


class DeskGlanceRouteTest(unittest.TestCase):
    """Actual renderGlanceView route: quiet poll keeps the live reading
    node, changed content re-fetches through the real loader, and a
    route change discards the late response."""

    def test_glance_quiet_poll_then_changed_refresh_then_route_guard(self):
        setup = ("DESK = %s; DETAIL = %s;" % (
            json.dumps(desk_payload()), json.dumps(detail_payload())))
        out = run_driver("""
(async () => {
  SUMMARY = {repos: [], served_at: null, at: null};
  await renderGlanceView();
  const row = viewBox.querySelector("[data-deskrow]");
  if (!row) throw new Error("no desk row in glance route");
  const list = viewBox.querySelector("[data-desklist]");
  list.scrollTop = 77;
  await row.click();
  await new Promise(r => setTimeout(r, 30));
  const paneNodeBefore = viewBox.querySelector("[data-deskpane]");
  const paneBefore = paneNodeBefore.innerHTML;
  const focusBefore = !!(document.activeElement && document.activeElement.dataset &&
    document.activeElement.dataset.deskhead === "cal:question:run-1");
  // Quiet poll: identical snapshot served again. The live pane node,
  // nonzero scroll, and focus must survive; inventory stays collapsed.
  await renderGlanceView();
  const quiet = {
    paneSame: viewBox.querySelector("[data-deskpane]").innerHTML === paneBefore,
    sameNode: viewBox.querySelector("[data-deskpane]") === paneNodeBefore,
    scroll: viewBox.querySelector("[data-desklist]").scrollTop === 77,
    focus: !!(document.activeElement && document.activeElement.dataset &&
      document.activeElement.dataset.deskhead === "cal:question:run-1"),
    focusExact: document.activeElement === viewBox.querySelector('[data-deskhead="cal:question:run-1"]'),
    invClosed: (viewBox.querySelectorAll('details[data-gk="inventory"]')[0] || {}).open !== true
  };
  // Observation-only change (served_at moves, content identical):
  // still the same live node and scroll position.
  SUMMARY = {repos: [], served_at: "2026-10-27T01:00:00+00:00", at: "2026-10-27T01:00:00+00:00"};
  await renderGlanceView();
  const obsOnly = {
    sameNode: viewBox.querySelector("[data-deskpane]") === paneNodeBefore,
    scroll: viewBox.querySelector("[data-desklist]").scrollTop === 77,
    noBadge: viewBox.innerHTML.indexOf("바뀐 근거가 반영됐습니다") < 0
  };
  // Changed content on a new revision.
  const desk2 = JSON.parse(JSON.stringify(DESK));
  desk2.revision = "2026-10-27T01:00:00+00:00";
  DESK = desk2;
  const edited = JSON.parse(JSON.stringify(DETAIL));
  edited.inputs.wait_reason = "question: glance edited";
  edited.question_full = "glance edited";
  DETAIL = edited;
  await renderGlanceView();
  const changed = {
    badge: viewBox.innerHTML.includes("바뀐 근거가 반영됐습니다"),
    content: viewBox.innerHTML.includes("glance edited"),
    gets: GETS.filter(u => u.startsWith("/api/decision/detail")).length,
    scroll: viewBox.querySelector("[data-desklist]").scrollTop === 77
  };
  // Route moved away: a late refresh must not repaint.
  PARSE_KIND = "repo";
  const desk3 = JSON.parse(JSON.stringify(DESK));
  desk3.revision = "2026-10-27T02:00:00+00:00";
  DESK = desk3;
  const edited2 = JSON.parse(JSON.stringify(edited));
  edited2.inputs.wait_reason = "question: route moved";
  edited2.question_full = "route moved";
  DETAIL = edited2;
  const root = viewBox.querySelector("#deskroot");
  await deskRefreshSelected(root);
  const guarded = {
    noMove: viewBox.innerHTML.indexOf("route moved") < 0,
    kept: viewBox.innerHTML.includes("glance edited")
  };
  console.log(JSON.stringify({focusBefore, quiet, obsOnly, changed, guarded}));
})();
""", setup, glance_js() + "\n" + GLANCE_STUBS)
        self.assertTrue(out[0]["focusBefore"])
        for key in ("paneSame", "sameNode", "scroll", "focus", "focusExact",
                    "invClosed"):
            self.assertTrue(out[0]["quiet"][key], "quiet." + key)
        for key in ("sameNode", "scroll", "noBadge"):
            self.assertTrue(out[0]["obsOnly"][key], "obsOnly." + key)
        self.assertTrue(out[0]["changed"]["badge"])
        self.assertTrue(out[0]["changed"]["content"])
        self.assertEqual(out[0]["changed"]["gets"], 2)
        self.assertTrue(out[0]["changed"]["scroll"])
        for key in ("noMove", "kept"):
            self.assertTrue(out[0]["guarded"][key], "guarded." + key)

    def test_glance_supported_count_change_updates_chrome(self):
        payload = desk_payload()
        payload["supported"].append({
            "id": "cal:question:run-2", "repo": "cal", "kind": "question",
            "ref": "run-2", "title_excerpt": "Second?", "title_is_excerpt": True,
            "age_days": 1, "age_text": "1일째 대기", "why": "", "suggested": "",
            "suggested_basis": "", "supported": True,
            "revision": "2026-10-27T01:00:00+00:00"})
        setup = "DESK = %s;" % json.dumps(desk_payload())
        out = run_driver("""
(async () => {
  SUMMARY = {repos: [], served_at: null, at: null};
  await renderGlanceView();
  const paneNode = viewBox.querySelector("[data-deskpane]");
  const desk2 = %s;
  desk2.revision = "2026-10-27T01:00:00+00:00";
  desk2.repos[0].counts = {need_you_total: 3, need_you_supported: 2, blocked_total: 1};
  DESK = desk2;
  await renderGlanceView();
  const html = viewBox.innerHTML;
  console.log(JSON.stringify({
    newRow: html.includes("Second?"),
    headCount: html.includes("읽기 전용 판단 2건"),
    railCount: html.includes("결정 2/3"),
    paneNodeKept: viewBox.querySelector("[data-deskpane]") === paneNode,
    paneGuide: viewBox.querySelector("[data-deskpane]").innerHTML.includes("목록에서 판단을 선택하세요")
  }));
})();
""" % json.dumps(payload), setup, glance_js() + "\n" + GLANCE_STUBS)
        for key in ("newRow", "headCount", "railCount", "paneNodeKept",
                    "paneGuide"):
            self.assertTrue(out[0][key], key)

    def test_glance_unknown_then_healthy_transition(self):
        setup = "DESK = %s;" % json.dumps({
            "unknown": True, "error": "snapshot boom", "repos": [],
            "decisions": [], "supported": [], "unsupported": [],
            "revision": None})
        out = run_driver("""
(async () => {
  SUMMARY = {repos: [], served_at: null, at: null};
  await renderGlanceView();
  const before = viewBox.innerHTML;
  DESK = %s;
  await renderGlanceView();
  const html = viewBox.innerHTML;
  console.log(JSON.stringify({
    wasError: before.includes("snapshot boom"),
    rowsBack: html.includes("#431 Foo?"),
    headCount: html.includes("읽기 전용 판단 1건"),
    noError: html.indexOf("snapshot boom") < 0,
    guide: html.includes("목록에서 판단을 선택하세요")
  }));
})();
""" % json.dumps(desk_payload()), setup, glance_js() + "\n" + GLANCE_STUBS)
        for key in ("wasError", "rowsBack", "headCount", "noError", "guide"):
            self.assertTrue(out[0][key], key)

    def test_glance_unknown_snapshot_is_error_not_healthy_empty(self):
        setup = "DESK = %s;" % json.dumps({
            "unknown": True, "error": "snapshot boom", "repos": [],
            "decisions": [], "supported": [], "unsupported": [],
            "revision": None})
        out = run_driver("""
(async () => {
  SUMMARY = {repos: [], served_at: null, at: null};
  await renderGlanceView();
  const html = viewBox.innerHTML;
  console.log(JSON.stringify({
    err: html.includes("snapshot boom"),
    retry: viewBox.querySelectorAll("[data-deskreload]").length >= 1,
    noHealthyEmpty: html.indexOf("대기 중인 결정이 없습니다") < 0,
    noRow: viewBox.querySelectorAll("[data-deskrow]").length === 0
  }));
})();
""", setup, glance_js() + "\n" + GLANCE_STUBS)
        for key in ("err", "retry", "noHealthyEmpty", "noRow"):
            self.assertTrue(out[0][key], key)


class DeskRevisionTest(unittest.TestCase):
    """Compact strip + reading pane revision: filter, dedupe, primary
    action, folded evidence, mobile back, and scroll restoration."""

    def test_filter_strip_limits_rows_and_keeps_honest_counts(self):
        payload = {
            "unknown": False, "error": None,
            "revision": "2026-10-27T00:00:00+00:00",
            "repos": [
                {"name": "nitro", "counts": {
                    "need_you_total": 2, "need_you_supported": 1,
                    "blocked_total": 1}},
                {"name": "cal", "counts": {
                    "need_you_total": 1, "need_you_supported": 1,
                    "blocked_total": 0}}],
            "decisions": [],
            "supported": [
                {"id": "nitro:question:run-1", "repo": "nitro",
                 "kind": "question", "ref": "run-1",
                 "title_excerpt": "Nitro Q?", "title_is_excerpt": True,
                 "age_days": 9, "age_text": "9일째 대기", "why": "멈춤",
                 "suggested": "", "suggested_basis": "", "supported": True,
                 "revision": "2026-10-27T00:00:00+00:00"},
                {"id": "cal:question:run-2", "repo": "cal",
                 "kind": "question", "ref": "run-2",
                 "title_excerpt": "Cal Q?", "title_is_excerpt": True,
                 "age_days": 1, "age_text": "1일째 대기", "why": "",
                 "suggested": "", "suggested_basis": "", "supported": True,
                 "revision": "2026-10-27T00:00:00+00:00"}],
            "unsupported": [
                {"id": "nitro:prepare-retry:run-9", "repo": "nitro",
                 "kind": "prepare-retry", "ref": "run-9",
                 "title_excerpt": "retry", "title_is_excerpt": True,
                 "age_days": 1, "age_text": "1일째", "why": "",
                 "suggested": "", "suggested_basis": "", "supported": False,
                 "support_note": "지원하지 않음",
                 "revision": "2026-10-27T00:00:00+00:00"}]}
        setup = "DESK = %s;" % json.dumps(payload)
        out = run_driver("""
(async () => {
""" + mount_root() + """
  const html = deskBox.innerHTML;
  const init = {
    twoRows: deskBox.querySelectorAll("[data-deskrow]").length === 2,
    threeFilters: deskBox.querySelectorAll("[data-deskrepo]").length === 3,
    nitroCount: html.includes("nitro 결정 1/2"),
    allPressed: html.includes('data-deskrepo="" aria-pressed="true"')
  };
  deskBox.querySelector('[data-deskrepo="nitro"]').click();
  const filteredHtml = deskBox.innerHTML;
  const filtered = {
    oneRow: deskBox.querySelectorAll("[data-deskrow]").length === 1,
    nitroOnly: deskBox.querySelector("[data-deskrow]").dataset.deskrow === "nitro:question:run-1",
    unsNitro: filteredHtml.includes("다른 종류 1건"),
    nitroPressed: filteredHtml.includes('data-deskrepo="nitro" aria-pressed="true"'),
    allReleased: filteredHtml.includes('data-deskrepo="" aria-pressed="false"')
  };
  const fallback = deskListInner(DESK, null, "nope");
  console.log(JSON.stringify({init, filtered,
    fallbackAll: (fallback.match(/data-deskrow/g) || []).length === 2}));
})();
""", setup)
        for key in ("twoRows", "threeFilters", "nitroCount", "allPressed"):
            self.assertTrue(out[0]["init"][key], "init." + key)
        for key in ("oneRow", "nitroOnly", "unsNitro", "nitroPressed"):
            self.assertTrue(out[0]["filtered"][key], "filtered." + key)
        self.assertTrue(out[0]["fallbackAll"])

    def test_identical_title_skips_duplicate_full_question(self):
        setup = "DESK = %s;" % json.dumps(desk_payload())
        out = run_driver("""
(async () => {
  const same = %s;
  same.title = same.question_full;
  const item = {id: same.id};
  const htmlSame = deskPaneHtml(same, item, false, same.id, "");
  const diff = %s;
  const htmlDiff = deskPaneHtml(diff, {id: diff.id}, false, diff.id, "");
  console.log(JSON.stringify({
    noDup: htmlSame.indexOf("질문 전문") < 0,
    kept: htmlSame.includes("full question text here"),
    shows: htmlDiff.includes("질문 전문") && htmlDiff.includes("full question text here")
  }));
})();
""" % (json.dumps(detail_payload()), json.dumps(detail_payload())), setup)
        for key in ("noDup", "kept", "shows"):
            self.assertTrue(out[0][key], key)

    def test_pane_primary_action_and_folded_evidence(self):
        setup = "DESK = %s;" % json.dumps(desk_payload())
        out = run_driver("""
(async () => {
  const det = %s;
  const html = deskPaneHtml(det, {id: det.id}, false, det.id, "");
  // Semantic DOM checks on the real parsed nodes: identity
  // attributes are legitimate, so assert node/attribute truth
  // instead of a byte-exact legacy fragment.
  const tmp = mkBox();
  tmp.innerHTML = html;
  const textOf = (el) => (el.children || []).filter(c => c.tag === "#text").map(c => c.text).join("");
  const primary = tmp.querySelector('[data-deskgo="primary"]');
  const issue = tmp.querySelector('[data-deskgo="issue"]');
  console.log(JSON.stringify({
    primary: !!(primary && primary.tag === "a" && primary.classList.contains("btn") &&
      !primary.classList.contains("sm") && primary._attrs.href === "#/work/run-1" &&
      textOf(primary) === "실행 기록에서 답하기"),
    issueSecondary: !!(issue && issue.tag === "a" && issue.classList.contains("btn") &&
      issue.classList.contains("sm") && issue._attrs.href === "https://github.com/o/cal/issues/431" &&
      textOf(issue) === "원문 이슈 보기"),
    stepsFolded: html.includes('data-gk="desk-steps"') && html.indexOf("triage:blocked") < 0,
    stepLabel: html.includes("분류"),
    srcFolded: html.includes('data-gk="desk-src"') && html.includes("%s"),
    topBack: html.includes("dd-topback") && html.includes("목록으로")
  }));
})();
""" % (json.dumps(detail_payload()), "f" * 64), setup)
        for key in ("primary", "issueSecondary", "stepsFolded",
                    "stepLabel", "srcFolded", "topBack"):
            self.assertTrue(out[0][key], key)

    def test_back_restores_select_time_scroll_positions(self):
        setup = ("DESK = %s; DETAIL = %s;" % (
            json.dumps(desk_payload()), json.dumps(detail_payload())))
        extra = """
let seenOuter = "unset";
function glanceScrollTop(box) { return 320; }
function restoreGlanceScroll(box, top) { seenOuter = top; }
"""
        out = run_driver("""
(async () => {
""" + mount_root() + """
  deskBox.querySelector("[data-desklist]").scrollTop = 200;
  await deskBox.querySelector("[data-deskrow]").click();
  await new Promise(r => setTimeout(r, 30));
  await deskBox.querySelector("[data-deskback]").click();
  await new Promise(r => setTimeout(r, 20));
  const ae = document.activeElement;
  console.log(JSON.stringify({
    list: deskBox.querySelector("[data-desklist]").scrollTop === 200,
    outer: seenOuter === 320,
    focusRow: !!(ae && ae.dataset && ae.dataset.deskrow === "cal:question:run-1")
  }));
})();
""", setup, extra)
        for key in ("list", "outer", "focusRow"):
            self.assertTrue(out[0][key], key)

    def test_repo_filter_focus_survives_quiet_poll(self):
        setup = "DESK = %s;" % json.dumps(desk_payload())
        out = run_driver("""
(async () => {
""" + mount_root() + """
  const all = deskBox.querySelector('[data-deskrepo=""]');
  all.focus();
  const wasFocused = document.activeElement === all;
  paintDeskInto(root, true);
  const after = deskBox.querySelector('[data-deskrepo=""]');
  console.log(JSON.stringify({
    wasFocused,
    kept: document.activeElement === after && after !== null,
    fresh: after !== all
  }));
})();
""", setup)
        for key in ("wasFocused", "kept", "fresh"):
            self.assertTrue(out[0][key], key)

    def test_same_row_reselect_keeps_origin_position(self):
        setup = ("DESK = %s; DETAIL = %s;" % (
            json.dumps(desk_payload()), json.dumps(detail_payload())))
        out = run_driver("""
(async () => {
""" + mount_root() + """
  deskBox.querySelector("[data-desklist]").scrollTop = 200;
  await deskBox.querySelector("[data-deskrow]").click();
  await new Promise(r => setTimeout(r, 30));
  deskBox.querySelector("[data-desklist]").scrollTop = 500;
  await deskBox.querySelector("[data-deskrow]").click();
  await new Promise(r => setTimeout(r, 30));
  await deskBox.querySelector("[data-deskback]").click();
  await new Promise(r => setTimeout(r, 20));
  console.log(JSON.stringify({
    backToOrigin: deskBox.querySelector("[data-desklist]").scrollTop === 200
  }));
})();
""", setup)
        self.assertTrue(out[0]["backToOrigin"])

    def test_back_restores_scroll_after_leaving_reading_mode(self):
        setup = ("DESK = %s; DETAIL = %s; NARROW = true;" % (
            json.dumps(desk_payload()), json.dumps(detail_payload())))
        out = run_driver("""
(async () => {
""" + mount_root() + """
  const grid = deskBox.querySelector(".dd-grid");
  const list = deskBox.querySelector("[data-desklist]");
  list.scrollTop = 150;
  let readingAtRestore = "unset";
  let cur = list.scrollTop;
  Object.defineProperty(list, "scrollTop", {
    get() { return cur; },
    set(v) { readingAtRestore = grid.classList.contains("dd-reading"); cur = v; },
    configurable: true
  });
  await deskBox.querySelector("[data-deskrow]").click();
  await new Promise(r => setTimeout(r, 30));
  const wasReading = grid.classList.contains("dd-reading");
  await deskBox.querySelector("[data-deskback]").click();
  await new Promise(r => setTimeout(r, 20));
  console.log(JSON.stringify({
    wasReading,
    visibleAtRestore: readingAtRestore === false,
    scroll: list.scrollTop === 150,
    leftReading: !grid.classList.contains("dd-reading")
  }));
})();
""", setup)
        for key in ("wasReading", "visibleAtRestore", "scroll",
                    "leftReading"):
            self.assertTrue(out[0][key], key)

    def test_step_values_human_readable_with_verbatim_fallback(self):
        setup = "DESK = %s;" % json.dumps(desk_payload())
        out = run_driver("""
(async () => {
  const det = %s;
  const html = deskPaneHtml(det, {id: det.id}, false, det.id, "");
  const other = JSON.parse(JSON.stringify(det));
  other.steps = {triage: "weird-state"};
  const htmlOther = deskPaneHtml(other, {id: other.id}, false, other.id, "");
  console.log(JSON.stringify({
    knownHuman: html.includes("분류") && html.includes("멈춤"),
    knownRawGone: html.indexOf("blocked") < 0,
    unknownVerbatim: htmlOther.includes("weird-state")
  }));
})();
""" % json.dumps(detail_payload()), setup)
        for key in ("knownHuman", "knownRawGone", "unknownVerbatim"):
            self.assertTrue(out[0][key], key)

    def test_recreated_grid_keeps_reading_mode_without_reselect(self):
        setup = ("DESK = %s; DETAIL = %s; NARROW = true;" % (
            json.dumps(desk_payload()), json.dumps(detail_payload())))
        out = run_driver("""
(async () => {
  SUMMARY = {repos: [], served_at: null, at: null};
  await renderGlanceView();
  await viewBox.querySelector("[data-deskrow]").click();
  await new Promise(r => setTimeout(r, 30));
  const detailGets = GETS.filter(u => u.startsWith("/api/decision/detail")).length;
  // Leave and re-enter glance: the grid is rebuilt from scratch while
  // the selection and cached detail survive in module state.
  viewBox.innerHTML = "";
  await renderGlanceView();
  const grid = viewBox.querySelector(".dd-grid");
  const row = viewBox.querySelector("[data-deskrow]");
  const after = {
    reading: !!(grid && grid.classList && grid.classList.contains("dd-reading")),
    selected: !!(row && row.dataset.deskrow === "cal:question:run-1" &&
      viewBox.innerHTML.includes('aria-current="true"')),
    paneDetail: viewBox.querySelector("[data-deskpane]").innerHTML.includes("full question text here"),
    noRefetch: GETS.filter(u => u.startsWith("/api/decision/detail")).length === detailGets
  };
  // Unknown snapshot and back to healthy: the mode must survive too.
  const keep = DESK;
  DESK = {unknown: true, error: "snapshot boom", repos: [],
    decisions: [], supported: [], unsupported: [], revision: null};
  await renderGlanceView();
  const gUnknown = viewBox.querySelector(".dd-grid");
  const duringUnknown = !!(gUnknown && gUnknown.classList &&
    gUnknown.classList.contains("dd-reading"));
  DESK = keep;
  await renderGlanceView();
  const gHealthy = viewBox.querySelector(".dd-grid");
  const afterHealthy = !!(gHealthy && gHealthy.classList &&
    gHealthy.classList.contains("dd-reading"));
  console.log(JSON.stringify({after, duringUnknown, afterHealthy}));
})();
""", setup, glance_js() + "\n" + GLANCE_STUBS)
        for key in ("reading", "selected", "paneDetail", "noRefetch"):
            self.assertTrue(out[0]["after"][key], "after." + key)
        self.assertTrue(out[0]["duringUnknown"])
        self.assertTrue(out[0]["afterHealthy"])

    def test_sync_reading_follows_narrow_and_selection(self):
        setup = ("DESK = %s; DETAIL = %s; NARROW = true;" % (
            json.dumps(desk_payload()), json.dumps(detail_payload())))
        out = run_driver("""
(async () => {
""" + mount_root() + """
  deskSyncReading(root);
  const noSel = !(deskBox.querySelector(".dd-grid").classList.contains("dd-reading"));
  await deskBox.querySelector("[data-deskrow]").click();
  await new Promise(r => setTimeout(r, 30));
  const onNarrow = deskBox.querySelector(".dd-grid").classList.contains("dd-reading");
  NARROW = false;
  deskSyncReading(root);
  const offWide = !(deskBox.querySelector(".dd-grid").classList.contains("dd-reading"));
  console.log(JSON.stringify({noSel, onNarrow, offWide}));
})();
""", setup)
        for key in ("noSel", "onNarrow", "offWide"):
            self.assertTrue(out[0][key], key)


class DeskFocusRepairTest(unittest.TestCase):
    """Repair A/B/C regressions: focus restore across content repaint,
    desk secondary-action hit area, inactive-message dedupe."""

    def test_changed_poll_restores_source_summary_focus_on_replaced_node(self):
        setup = ("DESK = %s; DETAIL = %s;" % (
            json.dumps(desk_payload()), json.dumps(detail_payload())))
        out = run_driver("""
(async () => {
""" + mount_root() + """
  await deskBox.querySelector("[data-deskrow]").click();
  await new Promise(r => setTimeout(r, 30));
  const det = deskBox.querySelector('details[data-gk="desk-src"]');
  if (!det) throw new Error("no source disclosure");
  det.open = true;
  const oldSummary = deskBox.querySelector('[data-desksec="src"]');
  if (!oldSummary) throw new Error("no src summary identity");
  oldSummary.focus();
  const desk2 = JSON.parse(JSON.stringify(DESK));
  desk2.revision = "2026-10-27T01:00:00+00:00";
  root._desk = desk2;
  const edited = JSON.parse(JSON.stringify(DETAIL));
  edited.inputs.wait_reason = "question: edited for focus check";
  edited.inputs.work_tree_fingerprint = "e".repeat(64);
  edited.question_full = "edited for focus check";
  DETAIL = edited;
  await deskRefreshSelected(root);
  const ae = document.activeElement;
  // Renderer preservation first: capture the open state exactly as
  // the repaint left it, before any manual open writes.
  const openKept = !!deskBox.querySelector('details[data-gk="desk-src"][open]');
  // Closed-state negative: the equality+open selector must stop
  // matching once the disclosure is closed, so it cannot always
  // return true.
  const detEl = deskBox.querySelector('details[data-gk="desk-src"]');
  detEl.open = false;
  const closedGone = !deskBox.querySelector('details[data-gk="desk-src"][open]');
  console.log(JSON.stringify({
    badge: deskBox.innerHTML.includes("바뀐 근거가 반영됐습니다"),
    focusKept: !!(ae && ae.tag === "summary" && ae.dataset && ae.dataset.desksec === "src"),
    replaced: (function () {
      const now = deskBox.querySelector('[data-desksec="src"]');
      return !!now && now !== oldSummary;
    })(),
    notBody: !(ae && ae.tag === undefined),
    openKept: openKept,
    closedGone: closedGone
  }));
})();
""", setup)
        for key in ("badge", "focusKept", "replaced", "openKept",
                    "closedGone"):
            self.assertTrue(out[0][key], key)

    def test_changed_poll_restores_steps_summary_and_action_links(self):
        setup = ("DESK = %s; DETAIL = %s;" % (
            json.dumps(desk_payload()), json.dumps(detail_payload())))
        out = run_driver("""
(async () => {
""" + mount_root() + """
  await deskBox.querySelector("[data-deskrow]").click();
  await new Promise(r => setTimeout(r, 30));
  const results = {};
  for (const [ix, kind] of ["steps", "primary", "issue"].entries()) {
    const sel = kind === "steps" ? '[data-desksec="steps"]'
      : kind === "primary" ? '[data-deskgo="primary"]' : '[data-deskgo="issue"]';
    const el = deskBox.querySelector(sel);
    if (!el) throw new Error("missing focus target " + kind);
    el.focus();
    // Each pass needs its own snapshot revision: after the first pass
    // the cache revision advances, so a repeated revision would
    // early-return and never replace the focused node.
    const desk2 = JSON.parse(JSON.stringify(DESK));
    desk2.revision = "2026-10-27T02:0" + ix + ":00+00:00";
    root._desk = desk2;
    const edited = JSON.parse(JSON.stringify(DETAIL));
    edited.inputs.wait_reason = "question: edited for " + kind;
    edited.question_full = "edited for " + kind;
    DETAIL = edited;
    await deskRefreshSelected(root);
    const ae = document.activeElement;
    results[kind] = !!(ae && ae !== el &&
      ((kind === "steps" && ae.tag === "summary" && ae.dataset && ae.dataset.desksec === "steps") ||
       (kind !== "steps" && ae.tag === "a" && ae.dataset && ae.dataset.deskgo === kind)));
  }
  console.log(JSON.stringify(results));
})();
""", setup)
        for key in ("steps", "primary", "issue"):
            self.assertTrue(out[0][key], key)

    def test_missing_summary_falls_back_to_same_pane_heading_only(self):
        setup = ("DESK = %s; DETAIL = %s;" % (
            json.dumps(desk_payload()), json.dumps(detail_payload())))
        out = run_driver("""
(async () => {
""" + mount_root() + """
  await deskBox.querySelector("[data-deskrow]").click();
  await new Promise(r => setTimeout(r, 30));
  const steps = deskBox.querySelector('[data-desksec="steps"]');
  if (!steps) throw new Error("no steps summary identity");
  steps.focus();
  // Changed content with no steps section: the focused summary is
  // gone, so focus must fall back to the same pane heading, never
  // to an unrelated row or outside the pane.
  const desk2 = JSON.parse(JSON.stringify(DESK));
  desk2.revision = "2026-10-27T03:00:00+00:00";
  root._desk = desk2;
  const edited = JSON.parse(JSON.stringify(DETAIL));
  edited.inputs.wait_reason = "question: steps removed";
  edited.inputs.steps = {};
  edited.steps = {};
  edited.question_full = "steps removed";
  DETAIL = edited;
  await deskRefreshSelected(root);
  const ae = document.activeElement;
  console.log(JSON.stringify({
    headFallback: !!(ae && ae.dataset && ae.dataset.deskhead === "cal:question:run-1"),
    noRowFocus: !(ae && ae.dataset && ae.dataset.deskrow)
  }));
})();
""", setup)
        for key in ("headFallback", "noRowFocus"):
            self.assertTrue(out[0][key], key)

    def test_quiet_poll_keeps_already_focused_summary_and_links(self):
        setup = ("DESK = %s; DETAIL = %s;" % (
            json.dumps(desk_payload()), json.dumps(detail_payload())))
        out = run_driver("""
(async () => {
""" + mount_root() + """
  await deskBox.querySelector("[data-deskrow]").click();
  await new Promise(r => setTimeout(r, 30));
  const targets = {
    src: '[data-desksec="src"]',
    steps: '[data-desksec="steps"]',
    primary: '[data-deskgo="primary"]',
    issue: '[data-deskgo="issue"]'
  };
  const kept = {};
  for (const kind of Object.keys(targets)) {
    const el = deskBox.querySelector(targets[kind]);
    if (!el) throw new Error("missing quiet target " + kind);
    el.focus();
    // Renderer path with an unchanged snapshot: the pane must not
    // repaint and the focused node must stay focused, not move to
    // the pane heading.
    paintDeskInto(root, true);
    const ae = document.activeElement;
    kept[kind] = ae === el;
  }
  console.log(JSON.stringify(kept));
})();
""", setup)
        for key in ("src", "steps", "primary", "issue"):
            self.assertTrue(out[0][key], key)

    def test_secondary_issue_action_meets_desk_hit_area(self):
        # Structural gate only: shipped desk CSS must give the secondary
        # issue action a 44px minimum hit height. Real pixel proof stays
        # a Root Chrome gate.
        src = Path(__file__).resolve().parent.parent / "src" / "openmainty" / "dashboard.html"
        html = src.read_text(encoding="utf-8")
        self.assertIn(".dd-pane .dd-evline .btn", html)
        rule = html.split(".dd-pane .dd-evline .btn", 1)[1].split("}", 1)[0]
        self.assertIn("min-height: 44px", rule)
        setup = ("DESK = %s; DETAIL = %s;" % (
            json.dumps(desk_payload()), json.dumps(detail_payload())))
        out = run_driver("""
(async () => {
""" + mount_root() + """
  await deskBox.querySelector("[data-deskrow]").click();
  await new Promise(r => setTimeout(r, 30));
  const link = deskBox.querySelector('[data-deskgo="issue"]');
  const pane = deskBox.querySelector("[data-deskpane]");
  console.log(JSON.stringify({
    issueLink: !!(link && link.tag === "a" && pane && (function () {
      try { return pane.innerHTML.includes("원문 이슈 보기"); } catch (_) { return false; }
    })()),
    inEvline: deskBox.innerHTML.includes("dd-evline")
  }));
})();
""", setup)
        for key in ("issueLink", "inEvline"):
            self.assertTrue(out[0][key], key)

    def test_inactive_selection_shows_single_guidance_message(self):
        out = run_driver("""
(() => {
  const html = deskPaneHtml({unknown: true, inactive: true,
    error: "최신 요약에 없는 항목입니다. 전체 목록에서 현재 상태를 확인하세요."},
    null, false, "cal:question:run-1", "");
  const count = html.split("최신 요약에 없는 항목입니다").length - 1;
  console.log(JSON.stringify({
    single: count === 1,
    back: html.includes("목록으로"),
    status: html.includes("최신 요약에 없는 항목입니다")
  }));
})();
""")
        for key in ("single", "back", "status"):
            self.assertTrue(out[0][key], key)

    def test_distinct_error_keeps_both_error_and_inactive_note(self):
        out = run_driver("""
(() => {
  const html = deskPaneHtml({unknown: true, inactive: true,
    error: "연결 실패"}, null, false, "cal:question:run-1", "");
  console.log(JSON.stringify({
    error: html.includes("연결 실패"),
    note: html.includes("최신 요약에 없는 항목입니다"),
    back: html.includes("목록으로")
  }));
})();
""")
        for key in ("error", "note", "back"):
            self.assertTrue(out[0][key], key)


FOOTER_SETUP = r"""
let APPFIX = null, APPFIXMODE = "ok", APPFIXWAITERS = [];
let DISKFIX = null;
const __baseJget = jget;
jget = function(url) {
  if (url === "/api/app") {
    GETS.push(url);
    return new Promise((res, rej) => setTimeout(() => {
      if (APPFIXMODE === "error") return rej(new Error("app offline"));
      if (APPFIXMODE === "deferred") { APPFIXWAITERS.push({res: res, rej: rej}); return; }
      if (APPFIX === null || APPFIX === undefined) return rej(new Error("no app fixture"));
      return res(JSON.parse(JSON.stringify(APPFIX)));
    }, 1));
  }
  if (url === "/api/disk") {
    GETS.push(url);
    return new Promise((res, rej) => setTimeout(() => {
      if (DISKFIX === null || DISKFIX === undefined) return rej(new Error("no disk fixture"));
      return res(JSON.parse(JSON.stringify(DISKFIX)));
    }, 1));
  }
  return __baseJget(url);
};
const __stubDollar = $;
$ = function(id) {
  if (id === "view") return viewBox;
  if (id === "bartitle") return {set textContent(v) {}};
  const hit = viewBox.querySelector("#" + id);
  return hit || null;
};
"""


class DeskFooterVersionTest(unittest.TestCase):
    """Glance footer running version: fresh /api/app, never baked snapshot.

    Drives the actual renderGlanceView route with controllable /api/app
    and /api/disk fixtures. The footer version node must show the fresh
    runtime sha, unknown on failure/malformed input, and update without
    disturbing the disk meter or Desk reading state.
    """

    DISK = ("DISKFIX = {free_bytes: 111000000000, home_bytes: "
            "164000000000, stale_s: 780};")

    def test_footer_shows_fresh_runtime_not_baked_snapshot(self):
        setup = ("DESK = %s; DETAIL = %s;" % (
            json.dumps(desk_payload()), json.dumps(detail_payload())))
        out = run_driver(r"""
(async () => {
  SUMMARY = {repos: [{name: "cal", system: {app_sha: "a2b3110544fddf49"}}],
    served_at: "t", at: "t"};
  APPFIX = {app_sha: "58248a11cb745058", dev_differs: false};
""" + "  " + self.DISK + r"""
  await renderGlanceView();
  await new Promise(r => setTimeout(r, 40));
  const v1 = viewBox.querySelector("#appver").textContent;
  const d1 = viewBox.querySelector("#appdiff").textContent;
  SUMMARY = {repos: [{name: "cal"}], served_at: "t2", at: "t2"};
  await renderGlanceView();
  await new Promise(r => setTimeout(r, 40));
  const v2 = viewBox.querySelector("#appver").textContent;
  console.log(JSON.stringify({v1: v1, d1: d1, v2: v2}));
})();
""", FOOTER_SETUP + setup, glance_js() + "\n" + GLANCE_STUBS)
        self.assertEqual(out[0]["v1"], "앱 58248a11cb74")
        self.assertNotIn("a2b311", out[0]["v1"])
        self.assertEqual(out[0]["d1"], "")
        self.assertEqual(out[0]["v2"], "앱 58248a11cb74")

    def test_footer_unknown_on_error_null_malformed(self):
        setup = "DESK = %s;" % json.dumps(desk_payload())
        out = run_driver(r"""
(async () => {
  SUMMARY = {repos: [], served_at: "t", at: "t"};
  APPFIXMODE = "error";
  await renderGlanceView();
  await new Promise(r => setTimeout(r, 40));
  const e1 = viewBox.querySelector("#appver").textContent;
  APPFIXMODE = "ok"; APPFIX = {app_sha: null, dev_differs: false};
  await renderGlanceView();
  await new Promise(r => setTimeout(r, 40));
  const e2 = viewBox.querySelector("#appver").textContent;
  APPFIX = {app_sha: "   ", dev_differs: false};
  await renderGlanceView();
  await new Promise(r => setTimeout(r, 40));
  const e3 = viewBox.querySelector("#appver").textContent;
  APPFIX = {app_sha: 12345, dev_differs: false};
  await renderGlanceView();
  await new Promise(r => setTimeout(r, 40));
  const e4 = viewBox.querySelector("#appver").textContent;
  APPFIX = {dev_differs: false};
  await renderGlanceView();
  await new Promise(r => setTimeout(r, 40));
  const e5 = viewBox.querySelector("#appver").textContent;
  console.log(JSON.stringify({e1: e1, e2: e2, e3: e3, e4: e4, e5: e5}));
})();
""", FOOTER_SETUP + setup, glance_js() + "\n" + GLANCE_STUBS)
        for key in ("e1", "e2", "e3", "e4", "e5"):
            self.assertEqual(out[0][key], "앱 버전 확인 불가", key)

    def test_footer_devdiff_set_and_cleared(self):
        setup = "DESK = %s;" % json.dumps(desk_payload())
        out = run_driver(r"""
(async () => {
  SUMMARY = {repos: [], served_at: "t", at: "t"};
  APPFIX = {app_sha: "58248a11cb745058", dev_differs: true};
  await renderGlanceView();
  await new Promise(r => setTimeout(r, 40));
  const d1 = viewBox.querySelector("#appdiff").textContent;
  APPFIX = {app_sha: "58248a11cb745058", dev_differs: false};
  await renderGlanceView();
  await new Promise(r => setTimeout(r, 40));
  const d2 = viewBox.querySelector("#appdiff").textContent;
  APPFIX = {app_sha: "58248a11cb745058", dev_differs: null};
  await renderGlanceView();
  await new Promise(r => setTimeout(r, 40));
  const d3 = viewBox.querySelector("#appdiff").textContent;
  console.log(JSON.stringify({d1: d1, d2: d2, d3: d3}));
})();
""", FOOTER_SETUP + setup, glance_js() + "\n" + GLANCE_STUBS)
        self.assertEqual(out[0]["d1"], "개발 트리와 다름")
        self.assertEqual(out[0]["d2"], "")
        self.assertEqual(out[0]["d3"], "")

    def test_footer_failure_clears_prior_warning_keeps_reading_state(self):
        setup = ("DESK = %s; DETAIL = %s;" % (
            json.dumps(desk_payload()), json.dumps(detail_payload())))
        out = run_driver(r"""
(async () => {
  SUMMARY = {repos: [], served_at: "t", at: "t"};
  APPFIX = {app_sha: "58248a11cb745058", dev_differs: true};
""" + "  " + self.DISK + r"""
  await renderGlanceView();
  await new Promise(r => setTimeout(r, 40));
  await viewBox.querySelector("[data-deskrow]").click();
  await new Promise(r => setTimeout(r, 30));
  const focusNode = document.activeElement;
  const verNode = viewBox.querySelector("#appver");
  const diffNode = viewBox.querySelector("#appdiff");
  const diskNode = viewBox.querySelector("#diskmeter");
  const diskText = diskNode && diskNode.textContent;
  const paneNode = viewBox.querySelector("[data-deskpane]");
  const warnBefore = diffNode && diffNode.textContent;
  APPFIXMODE = "error";
  await renderGlanceView();
  await new Promise(r => setTimeout(r, 40));
  console.log(JSON.stringify({
    warnBefore: warnBefore,
    ver: viewBox.querySelector("#appver").textContent,
    diff: viewBox.querySelector("#appdiff").textContent,
    verSame: viewBox.querySelector("#appver") === verNode,
    diffSame: viewBox.querySelector("#appdiff") === diffNode,
    diskSame: viewBox.querySelector("#diskmeter") === diskNode,
    diskText: viewBox.querySelector("#diskmeter") &&
      viewBox.querySelector("#diskmeter").textContent === diskText,
    focusSame: document.activeElement === focusNode,
    paneSame: viewBox.querySelector("[data-deskpane]") === paneNode,
    appGets: GETS.filter(u => u === "/api/app").length,
    diskGets: GETS.filter(u => u === "/api/disk").length
  }));
})();
""", FOOTER_SETUP + setup, glance_js() + "\n" + GLANCE_STUBS)
        self.assertEqual(out[0]["warnBefore"], "개발 트리와 다름")
        self.assertEqual(out[0]["ver"], "앱 버전 확인 불가")
        self.assertEqual(out[0]["diff"], "")
        for key in ("verSame", "diffSame", "diskSame", "diskText",
                    "focusSame", "paneSame"):
            self.assertTrue(out[0][key], key)
        self.assertEqual(out[0]["appGets"], 2)
        self.assertEqual(out[0]["diskGets"], 2)

    def test_footer_late_response_and_route_change_dropped(self):
        setup = "DESK = %s;" % json.dumps(desk_payload())
        out = run_driver(r"""
(async () => {
  SUMMARY = {repos: [], served_at: "t", at: "t"};
  APPFIXMODE = "deferred";
  APPFIX = {app_sha: "58248a11cb745058", dev_differs: false};
  const p1 = renderGlanceView();
  await p1;
  await new Promise(r => setTimeout(r, 10));
  const pending1 = APPFIXWAITERS.length;
  APPFIXMODE = "ok";
  APPFIX = {app_sha: "99990a11cb745099", dev_differs: false};
  await renderGlanceView();
  await new Promise(r => setTimeout(r, 40));
  const midVer = viewBox.querySelector("#appver").textContent;
  APPFIXWAITERS[0].res({app_sha: "a2b3110544fddf49", dev_differs: true});
  await new Promise(r => setTimeout(r, 20));
  const lateVer = viewBox.querySelector("#appver").textContent;
  const lateDiff = viewBox.querySelector("#appdiff").textContent;
  APPFIXMODE = "deferred";
  const wBefore = APPFIXWAITERS.length;
  const p3 = renderGlanceView();
  await p3;
  await new Promise(r => setTimeout(r, 10));
  PARSE_KIND = "repo";
  APPFIXWAITERS[wBefore].res({app_sha: "bbbb11cb7450bbbb", dev_differs: true});
  await new Promise(r => setTimeout(r, 20));
  const routeVer = viewBox.querySelector("#appver").textContent;
  PARSE_KIND = "glance";
  console.log(JSON.stringify({pending1: pending1, midVer: midVer,
    lateVer: lateVer, lateDiff: lateDiff, routeVer: routeVer}));
})();
""", FOOTER_SETUP + setup, glance_js() + "\n" + GLANCE_STUBS)
        self.assertEqual(out[0]["pending1"], 1)
        self.assertEqual(out[0]["midVer"], "앱 99990a11cb74")
        self.assertEqual(out[0]["lateVer"], "앱 99990a11cb74")
        self.assertEqual(out[0]["lateDiff"], "")
        self.assertEqual(out[0]["routeVer"], "앱 99990a11cb74")

    def test_footer_absent_nodes_do_not_throw(self):
        setup = "DESK = %s;" % json.dumps(desk_payload())
        out = run_driver(r"""
(async () => {
  SUMMARY = {repos: [], served_at: "t", at: "t"};
  APPFIX = {app_sha: "58248a11cb745058", dev_differs: true};
  await renderGlanceView();
  await new Promise(r => setTimeout(r, 40));
  console.log(JSON.stringify({
    rows: viewBox.querySelectorAll("[data-deskrow]").length,
    checking: viewBox.querySelector("#appver").textContent,
    gets: GETS.filter(u => u === "/api/app").length
  }));
})();
// NOTE: no $ override here — the stock stub resolves no footer ids,
// so this proves the shipped guards never throw on absent nodes.
""", setup, glance_js() + "\n" + GLANCE_STUBS)
        self.assertGreaterEqual(out[0]["rows"], 1)
        self.assertEqual(out[0]["checking"], "앱 버전 확인 중…")
        self.assertEqual(out[0]["gets"], 1)


class DeskGuideWidthNeutralTest(unittest.TestCase):
    # G8: the empty-desk hint must not name a direction — at 390px the
    # list sits above the hint, not to its left.
    def test_guide_has_no_left_right_wording(self):
        src = Path(__file__).resolve().parent.parent / "src" / "openmainty" / "dashboard.html"
        html = src.read_text(encoding="utf-8")
        line = next(ln for ln in html.splitlines()
                    if "dd-empty" in ln and "판단을 선택하세요" in ln)
        self.assertIn("목록에서 판단을 선택하세요", line)
        for banned in ("왼쪽", "오른쪽", "좌측", "우측"):
            self.assertNotIn(banned, line)


if __name__ == "__main__":
    unittest.main()

