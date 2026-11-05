"""PR inbox shipped JS: real extracted dashboard functions in node.

Drives the actual paintRepoPrs/prRowHtml/prFp/prDetailHtml code from
dashboard.html against canned server payloads. Offline; no network.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


NODE_PRELUDE = r"""
let view = {id: "r"};
let repoTab = "prs";
const CSS = {escape: (s) => String(s)};
let confirmCalls = [];
function confirm(m) { confirmCalls.push(String(m)); return true; }
let toasts = [];
function toast(m) { toasts.push(String(m)); }
function mkEl(box, tag, attrs) {
  const el = {tag, _h: null, _attrs: attrs,
    set onclick(f) { this._h = f; }, get onclick() { return this._h; },
    click() { if (this._h) return this._h(); },
    dataset: {}, disabled: false, value: "",
    checked: false, textContent: "", onkeydown: null, oninput: null,
    ontoggle: null,
    focus() { box._focused = this; }};
  for (const [k, v] of Object.entries(attrs)) {
    if (k.startsWith("data-")) el.dataset[k.slice(5)] = v;
  }
  if (tag === "input" && attrs.value !== undefined) el.value = attrs.value;
  if (tag === "details") el.open = attrs.__open === true;
  el.querySelectorAll = (s) => [];
  el.querySelector = (s) => null;
  return el;
}
function parseEls(box, html) {
  const els = [];
  const re = /<(button|input|details|textarea|select)\b([^>]*)>/g;
  let m;
  while ((m = re.exec(html))) {
    const tag = m[1], attrText = m[2];
    const attrs = {};
    const are = /([a-zA-Z-]+)="([^"]*)"/g;
    let a;
    while ((a = are.exec(attrText))) attrs[a[1]] = a[2];
    attrs.__open = /(?:^|\s)open(?:\s|=|\/|>|$)/.test(attrText);
    els.push(mkEl(box, tag, attrs));
  }
  return els;
}
function mkBox() {
  const box = {_html: "", _cache: null, _focused: null,
    set innerHTML(v) { this._html = String(v); this._cache = null; },
    get innerHTML() { return this._html; },
    dataset: {},
    _els() {
      if (!this._cache) this._cache = parseEls(this, this._html);
      return this._cache;
    },
    querySelectorAll(sel) {
      const els = this._els();
      let tag = null, rest = sel;
      const tm = sel.match(/^([a-z]+)\[data-/);
      if (tm) { tag = tm[1]; rest = sel.slice(tag.length); }
      if (rest === "[data-dk]") {
        return els.filter(e => e.tag === (tag || "details") &&
          e.dataset.dk !== undefined);
      }
      const mo = rest.match(/^\[data-([a-z-]+)\]\[open\]$/);
      if (mo) {
        return els.filter(e => (!tag || e.tag === tag) &&
          e.dataset[mo[1]] !== undefined && e.open);
      }
      let m = rest.match(/^\[data-([a-z]+)="([^"]+)"\]$/);
      if (m) {
        return els.filter(e =>
          (!tag || e.tag === tag) && e.dataset[m[1]] === m[2]);
      }
      m = rest.match(/^\[data-([a-z]+)\]$/);
      if (m) {
        return els.filter(e =>
          (!tag || e.tag === tag) && e.dataset[m[1]] !== undefined);
      }
      const q = rest.match(/^\[data-k='([^']+)'\]$/);
      if (q) return els.filter(e => e.dataset.k === q[1]);
      return [];
    },
    querySelector(sel) {
      if (sel === "#pr-inspector") {
        if (!this._html.includes('id="pr-inspector"')) return null;
        const b = this;
        const card = {
          querySelectorAll: (s) => b.querySelectorAll(s), dataset: {},
          focus() { b._focused = "#pr-inspector"; }};
        card.querySelector = (s) => {
          const all = card.querySelectorAll(s);
          return all.length ? all[0] : null;
        };
        return card;
      }
      const all = this.querySelectorAll(sel);
      return all.length ? all[0] : null;
    }};
  return box;
}
const tabEl = mkBox();
function $(id) { return tabEl; }
let LIST = null, NODE = null, ATTEMPT = null, REVIEW_RESP = {started: true};
let DETAIL_MODE = "ok";
let postBodies = {};
let EVVALUE = "";
const document = {activeElement: null, querySelector(sel) {
  const found = tabEl.querySelector(sel);
  if (found) return found;
  const m = String(sel).match(/\[data-prevs="([^"]+)"\]/);
  if (m) return {value: EVVALUE};
  return null;
}};
let GETS = [];
function jget(url) {
  GETS.push(url);
  return new Promise((res, rej) => setTimeout(() => {
    if (url.startsWith("/api/prs?repo=")) return res(JSON.parse(JSON.stringify(LIST)));
    if (url.startsWith("/api/prs/detail")) {
      if (typeof DETAIL_MODE !== "undefined" && DETAIL_MODE === "error")
        return rej(new Error("상세 없음"));
      if (NODE instanceof Error) return rej(NODE);
      return res(JSON.parse(JSON.stringify(NODE)));
    }
    if (url.startsWith("/api/prs/attempt")) return res(JSON.parse(JSON.stringify(ATTEMPT)));
    return rej(new Error("unexpected GET " + url));
  }, 1));
}
function jpost(url, body) {
  postBodies[url] = body;
  return new Promise((res, rej) => setTimeout(() => {
    if (url === "/api/prs/fetch") return res({ok: true});
    if (url === "/api/prs/review") return res(JSON.parse(JSON.stringify(REVIEW_RESP)));
    return rej(new Error("unexpected POST " + url));
  }, 1));
}
"""


def pr_js():
    src = Path(__file__).resolve().parent.parent / "src" / "openmainty" / "dashboard.html"
    html = src.read_text(encoding="utf-8")
    esc_line = next(line for line in html.splitlines()
                    if line.startswith("function esc("))
    start = html.index("/* PR \uac80\ud1a0\ud568 \uc0c1\ud0dc")
    end = html.index("async function paintRepoCommunity(r) {", start)
    # Real shipped capture/restore helpers (scroller + rev-queue +
    # capture + stash/take + restore + async restore). They reference
    # heavier loaders only on branches the PR surface never takes
    # (no [data-changes]/[data-chgpath] slots), so the real focus,
    # details-open and scroll-top behavior runs in these tests while
    # input/caret/scroll preservation limits come from the fake DOM
    # (no layout metrics, no selection API), stated per test.
    hstart = html.index("function scrollerOf(el) {")
    hend = html.index("/* 묶음 카드용 다음 행동")
    helpers = html[hstart:hend]
    inflight = next(line for line in html.splitlines()
                    if line.startswith("const inflightGets"))
    di = html.index("function jgetDedupe(")
    dj = html.index("{", di)
    depth = 0
    for k in range(dj, len(html)):
        if html[k] == "{":
            depth += 1
        elif html[k] == "}":
            depth -= 1
            if depth == 0:
                dedupe = html[di:k + 1]
                break
    else:
        raise AssertionError("unbalanced jgetDedupe")
    return (esc_line + "\n" + inflight + "\n" + dedupe + "\n"
            + helpers + "\n" + html[start:end])


def run_driver(driver_js, setup_js=""):
    import subprocess
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as fh:
        # Fixture assignments run AFTER the extracted declarations so
        # setup never hits a temporal-dead-zone `let` in the product.
        fh.write(NODE_PRELUDE + pr_js() + "\n" + setup_js + driver_js)
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


def list_payload(items):
    return {"repo": "r", "slug": "o/r",
            "items": items,
            "model": {"ok": True, "reason": None}}


def row(num, bot=False, status="unreviewed", title="T"):
    return {"number": num, "title": title, "author": "c", "bot": bot,
            "is_draft": False, "head": "fix", "closes": [344],
            "additions": 10, "deletions": 2, "files": 1,
            "created_at": "t", "updated_at": "t",
            "review": {"status": status, "stale": False, "partial": False,
                       "job_status": "idle", "job_error": None,
                       "content_fp": "fp%d" % num}}


def node_payload(num=857, status="unreviewed", job="idle", report=None,
                 history=None):
    return {"repo": "r", "slug": "o/r",
            "linked_runs": {"344": None},
            "snapshot": {
                "number": num, "head_sha": "a" * 40,
                "base_sha": "b" * 40,
                "fetched_at": "2026-10-26T00:00:00+00:00",
                "detail": {
                    "number": num, "title": "T", "body": "B",
                    "author": "c", "state": "OPEN", "is_draft": False,
                    "created_at": "t", "updated_at": "t",
                    "closed_at": None, "merged_at": None,
                    "head_ref": "fix", "base_ref": "main",
                    "head_sha": "a" * 40, "base_sha": "b" * 40,
                    "url": "https://github.com/o/r/pull/%d" % num,
                    "labels": ["bug"], "labels_note": None,
                    "comment_count": 0, "comments": [],
                    "comments_complete": True, "comments_note": None,
                    "reviews": [], "reviews_complete": True,
                    "reviews_note": None, "closes": [344],
                    "additions": 10, "deletions": 2, "changed_files": 1,
                    "files": [{"path": "src/a.ts", "additions": 10,
                               "deletions": 2}],
                    "files_note": None},
                "diff": "d", "diff_bytes_total": 1,
                "diff_truncated": False, "checks": [],
                "checks_truncated": False, "threads": [], "partial": []},
            "review": {"status": status, "stale": False, "partial": False,
                       "content_fp": "fp", "report": report, "labels": {},
                       "source_unavailable": False,
                       "job": {"status": job}, "history": history or []},
            "model": {"ok": True, "reason": None}}


class TestInboxFlow(unittest.TestCase):
    def test_humans_first_bots_collapsed(self):
        setup = ("LIST = %s; NODE = %s;" % (
            json.dumps(list_payload(
                [row(1, bot=True), row(857)])),
            json.dumps(node_payload())))
        out = run_driver("""
(async () => {
  await paintRepoPrs({name: "r"});
  const html = $("tabbody").innerHTML;
  const humans = html.indexOf("사람 PR");
  const bots = html.indexOf("봇 PR");
  const firstHuman = html.indexOf("PR #857");
  const firstBot = html.indexOf("PR #1");
  console.log(JSON.stringify({humans, bots, firstHuman, firstBot,
    inspCount: (html.match(/id="pr-inspector"/g) || []).length,
    guidance: html.includes("PR을 선택하세요"),
    noReviewAction: !html.includes("data-prreview")}));
})();
""", setup)
        self.assertGreater(out[0]["humans"], -1)
        self.assertGreater(out[0]["bots"], -1)
        self.assertLess(out[0]["firstHuman"], out[0]["firstBot"])
        self.assertEqual(out[0]["inspCount"], 1)
        self.assertTrue(out[0]["guidance"])
        self.assertTrue(out[0]["noReviewAction"])

    def test_select_opens_one_inspector_with_identity(self):
        setup = ("LIST = %s; NODE = %s;" % (
            json.dumps(list_payload([row(857)])),
            json.dumps(node_payload())))
        out = run_driver("""
(async () => {
  await paintRepoPrs({name: "r"});
  const btns = $("tabbody").querySelectorAll("[data-prrowbtn]");
  if (btns.length !== 1) throw new Error("want 1 row, got " + btns.length);
  await btns[0].click();
  await new Promise(r => setTimeout(r, 20));
  const html = $("tabbody").innerHTML;
  console.log(JSON.stringify({
    title: html.includes("GitHub에서 원문 보기"),
    head: html.includes("a".repeat(12)),
    back: html.includes("data-prback"),
    current: html.includes('aria-current="true"'),
    inspCount: (html.match(/id="pr-inspector"/g) || []).length}));
})();
""", setup)
        self.assertTrue(out[0]["title"])
        self.assertTrue(out[0]["head"])
        self.assertTrue(out[0]["back"])
        self.assertTrue(out[0]["current"])
        self.assertEqual(out[0]["inspCount"], 1)

    def test_fetch_posts_confirm_and_clears_nodes(self):
        setup = ("LIST = %s; NODE = %s;" % (
            json.dumps(list_payload([row(857)])),
            json.dumps(node_payload())))
        out = run_driver("""
(async () => {
  await paintRepoPrs({name: "r"});
  const btns = $("tabbody").querySelectorAll("[data-prrowbtn]");
  await btns[0].click();
  await new Promise(r => setTimeout(r, 20));
  const fb = $("tabbody").querySelectorAll("[data-prfetch]");
  await fb[0].click();
  await new Promise(r => setTimeout(r, 20));
  console.log(JSON.stringify({fetch: postBodies["/api/prs/fetch"],
    toast: toasts.join("|")}));
})();
""", setup)
        self.assertEqual(out[0]["fetch"],
                         {"repo": "r", "number": "857", "confirm": True})

    def test_review_posts_evidence_and_confirm(self):
        setup = ("LIST = %s; NODE = %s;"
                 % (json.dumps(list_payload([row(857)])),
                    json.dumps(node_payload())))
        out = run_driver("""
(async () => {
  await paintRepoPrs({name: "r"});
  const btns = $("tabbody").querySelectorAll("[data-prrowbtn]");
  await btns[0].click();
  await new Promise(r => setTimeout(r, 20));
  const inp = $("tabbody").querySelectorAll("[data-prevs]")[0];
  inp.value = "src/a.ts, src/b.ts";
  const rb = $("tabbody").querySelectorAll("[data-prreview]");
  await rb[0].click();
  await new Promise(r => setTimeout(r, 20));
  console.log(JSON.stringify({review: postBodies["/api/prs/review"],
    confirms: confirmCalls.length}));
})();
""", setup)
        self.assertEqual(out[0]["review"]["evidence"],
                         ["src/a.ts", "src/b.ts"])
        self.assertEqual(out[0]["review"]["by"], "owner")
        self.assertTrue(out[0]["review"]["confirm"])
        self.assertEqual(out[0]["confirms"], 1)

    def test_fp_changes_with_job_status(self):
        out = run_driver("""
(() => {
  const a = prFp("r", {items: [{number: 1,
    review: {status: "unreviewed", job_status: "idle"}}]});
  const b = prFp("r", {items: [{number: 1,
    review: {status: "unreviewed", job_status: "running"}}]});
  console.log(JSON.stringify({diff: a !== b}));
})();
""")
        self.assertTrue(out[0]["diff"])

    def test_first_use_missing_then_fetch_then_review(self):
        setup = ("LIST = %s; NODE = %s; DETAIL_MODE = 'error';" % (
            json.dumps(list_payload([row(857)])),
            json.dumps(node_payload())))
        out = run_driver("""
(async () => {
  await paintRepoPrs({name: "r"});
  let btns = $("tabbody").querySelectorAll("[data-prrowbtn]");
  await btns[0].click();
  await new Promise(r => setTimeout(r, 20));
  let html = $("tabbody").innerHTML;
  const missingPanel = html.includes("가져오기") && !html.includes("읽기 전용 검토 시작");
  const fb = $("tabbody").querySelectorAll("[data-prfetch]");
  DETAIL_MODE = "ok";
  await fb[0].click();
  await new Promise(r => setTimeout(r, 40));
  html = $("tabbody").innerHTML;
  const hasReview = html.includes("읽기 전용 검토 시작");
  const rb = $("tabbody").querySelectorAll("[data-prreview]");
  await rb[0].click();
  await new Promise(r => setTimeout(r, 20));
  console.log(JSON.stringify({missingPanel, hasReview,
    fetch: postBodies["/api/prs/fetch"],
    review: postBodies["/api/prs/review"]}));
})();
""", setup)
        self.assertTrue(out[0]["missingPanel"])
        self.assertTrue(out[0]["hasReview"])
        self.assertEqual(out[0]["fetch"],
                         {"repo": "r", "number": "857", "confirm": True})
        self.assertEqual(out[0]["review"]["number"], "857")
        self.assertTrue(out[0]["review"]["confirm"])

    def test_running_to_done_refreshes_detail(self):
        running_list = list_payload(
            [dict(row(857), review=dict(row(857)["review"],
                                       job_status="running"))])
        done_list = list_payload(
            [dict(row(857), review=dict(row(857)["review"],
                                       status="reviewed",
                                       job_status="idle",
                                       content_fp="fp2"))])
        setup = ("LIST = %s; NODE = %s;" % (
            json.dumps(running_list), json.dumps(node_payload(job="running"))))
        out = run_driver("""
(async () => {
  await paintRepoPrs({name: "r"});
  const btns = $("tabbody").querySelectorAll("[data-prrowbtn]");
  await btns[0].click();
  await new Promise(r => setTimeout(r, 20));
  const before = $("tabbody").innerHTML.includes("검토 실행 중");
  LIST = %s;
  NODE = %s;
  prFetchedAt = {};
  await paintRepoPrs({name: "r"});
  await new Promise(r => setTimeout(r, 20));
  const html = $("tabbody").innerHTML;
  console.log(JSON.stringify({before,
    after: html.includes("좋아요") || html.includes("권장"),
    runningGone: !html.includes("검토 실행 중")}));
})();
""" % (json.dumps(done_list),
       json.dumps(node_payload(
           status="reviewed",
           report={"summary": "좋아요", "findings": ["f"],
                   "labels": {"status": ["needs-review"],
                              "platform": ["ios"], "type": ["bug"]},
                   "evidence": [], "reply_draft": None,
                   "source_files": [],
                   "recommendation": "needs-changes",
                   "recommendation_basis": "b"}))), setup)
        self.assertTrue(out[0]["before"])
        self.assertTrue(out[0]["after"])
        self.assertTrue(out[0]["runningGone"])

    def test_attempt_unwrap_and_retry(self):
        setup = ("LIST = %s; NODE = %s; ATTEMPT = %s;" % (
            json.dumps(list_payload([row(857)])),
            json.dumps(node_payload(
                # Rendering-unit fixture: validated readable stored
                # attempt, so the attempt link renders (ghost-artifact
                # hiding is covered by the dedicated missing/corrupt
                # tests, not by stripping this flag here).
                history=[{"at": "t", "event": "review-error",
                          "rid": "rid-1", "error": "e",
                          "validated": True}])),
            json.dumps({"repo": "r", "slug": "o/r", "attempt": {
                "rid": "rid-1", "outcome": "reviewed",
                "report": {"summary": "S",
                           "recommendation": "needs-changes"},
                "input_snapshot": {
                    "head_sha": "a" * 40, "base_sha": "b" * 40,
                    "source_census": {
                        "delivered": [{"path": "src/a.ts",
                                       "head_sent": 9,
                                       "head_sha256": "h" * 64,
                                       "base_sent": 0}],
                        "omitted": [{"path": "x.ts",
                                     "reason": "nope"}]},
                    "partial": ["p1"]}}})))
        out = run_driver("""
(async () => {
  await paintRepoPrs({name: "r"});
  const btns = $("tabbody").querySelectorAll("[data-prrowbtn]");
  await btns[0].click();
  await new Promise(r => setTimeout(r, 20));
  const atts = $("tabbody").querySelectorAll("[data-pratt]");
  await atts[0].click();
  await new Promise(r => setTimeout(r, 20));
  const html = $("tabbody").innerHTML;
  console.log(JSON.stringify({
    shows: html.includes("시도 rid-1") && html.includes("권장"),
    census: html.includes("src/a.ts") && html.includes("제외: x.ts"),
    noRaw: !html.includes("bytes_total")}));
})();
""", setup)
        self.assertTrue(out[0]["shows"])
        self.assertTrue(out[0]["census"])
        self.assertTrue(out[0]["noRaw"])

    def test_attempt_stored_error_needs_no_reload(self):
        setup = ("LIST = %s; NODE = %s; ATTEMPT = %s;" % (
            json.dumps(list_payload([row(857)])),
            json.dumps(node_payload(
                # Rendering-unit fixture: validated readable stored
                # failed attempt (see comment in unwrap test).
                history=[{"at": "t", "event": "review-error",
                          "rid": "rid-9", "error": "boom",
                          "validated": True}])),
            json.dumps({"repo": "r", "slug": "o/r",
                        "attempt": {"rid": "rid-9", "outcome": "error",
                                    "error": "boom"}})))
        out = run_driver("""
(async () => {
  await paintRepoPrs({name: "r"});
  const btns = $("tabbody").querySelectorAll("[data-prrowbtn]");
  await btns[0].click();
  await new Promise(r => setTimeout(r, 20));
  const atts = $("tabbody").querySelectorAll("[data-pratt]");
  await atts[0].click();
  await new Promise(r => setTimeout(r, 20));
  const html = $("tabbody").innerHTML;
  console.log(JSON.stringify({
    showsErr: html.includes("boom"),
    noRetry: !html.includes("다시 불러오기")}));
})();
""", setup)
        self.assertTrue(out[0]["showsErr"])
        self.assertTrue(out[0]["noRetry"])

    def test_attempt_load_failure_shows_retry(self):
        setup = ("LIST = %s; NODE = %s; ATTEMPT = null;" % (
            json.dumps(list_payload([row(857)])),
            json.dumps(node_payload(
                # Rendering-unit fixture: validated readable stored
                # attempt (see comment in unwrap test).
                history=[{"at": "t", "event": "review-error",
                          "rid": "rid-9", "error": "boom",
                          "validated": True}]))))
        out = run_driver("""
(async () => {
  await paintRepoPrs({name: "r"});
  const btns = $("tabbody").querySelectorAll("[data-prrowbtn]");
  await btns[0].click();
  await new Promise(r => setTimeout(r, 20));
  const atts = $("tabbody").querySelectorAll("[data-pratt]");
  await atts[0].click();
  await new Promise(r => setTimeout(r, 20));
  let html = $("tabbody").innerHTML;
  const retry = html.includes("다시 불러오기");
  const gets0 = GETS.filter(u => u.startsWith("/api/prs/attempt")).length;
  const rb = $("tabbody").querySelectorAll("[data-prattretry]");
  ATTEMPT = {repo: "r", slug: "o/r",
    attempt: {rid: "rid-9", outcome: "error",
              error: "stored-failure-419"}};
  if (rb.length) { await rb[0].click(); await new Promise(r => setTimeout(r, 40)); }
  html = $("tabbody").innerHTML;
  const gets1 = GETS.filter(u => u.startsWith("/api/prs/attempt")).length;
  console.log(JSON.stringify({retry,
    refetched: gets1 > gets0,
    recovered: html.includes("stored-failure-419"),
    retryGone: !$("tabbody").querySelector("[data-prattretry]")}));
})();
""", setup)
        self.assertTrue(out[0]["retry"])
        self.assertTrue(out[0]["refetched"])
        self.assertTrue(out[0]["recovered"])
        self.assertTrue(out[0]["retryGone"])

    def test_keyboard_and_evidence_and_open_state(self):
        def node858(title):
            node = node_payload(num=858)
            node["snapshot"]["detail"] = dict(
                node["snapshot"]["detail"], number=858, title=title)
            return node

        first = node858("T1")
        changed = node858("T2-changed")
        setup = ("LIST = %s; NODE = %s;" % (
            json.dumps(list_payload([row(857),
                                     dict(row(858), title="T1")])),
            json.dumps(first)))
        out = run_driver("""
(async () => {
  await paintRepoPrs({name: "r"});
  let btns = $("tabbody").querySelectorAll("[data-prrowbtn]");
  btns[0].onkeydown({key: "ArrowDown", preventDefault() {}});
  const focusedSecond = $("tabbody")._focused === btns[1];
  await btns[1].click();
  await new Promise(r => setTimeout(r, 20));
  const inp = $("tabbody").querySelectorAll("[data-prevs]")[0];
  inp.value = "src/a.ts";
  inp.oninput();
  const det = $("tabbody").querySelector("#pr-inspector")
    .querySelectorAll("details[data-dk]");
  const body = det.find(d => d.dataset.dk === "pr-body");
  body.open = true;
  const listGets0 = GETS.filter(u => u.startsWith("/api/prs?")).length;
  const detGets0 = GETS.filter(u => u.startsWith("/api/prs/detail")).length;
  LIST = %s;
  NODE = %s;
  prFetchedAt = {};
  await paintRepoPrs({name: "r"});
  await new Promise(r => setTimeout(r, 20));
  const html = $("tabbody").innerHTML;
  const listGets1 = GETS.filter(u => u.startsWith("/api/prs?")).length;
  const detGets1 = GETS.filter(u => u.startsWith("/api/prs/detail")).length;
  const inp2 = $("tabbody").querySelectorAll("[data-prevs]")[0];
  const det2 = $("tabbody").querySelector("#pr-inspector")
    .querySelectorAll("details[data-dk]");
  const body2 = det2.find(d => d.dataset.dk === "pr-body");
  console.log(JSON.stringify({
    focusedSecond, evKept: inp2.value === "src/a.ts",
    openKept: body2.open === true,
    listRefetched: listGets1 > listGets0,
    detailRefetched: detGets1 > detGets0,
    newTitle: html.includes("T2-changed")}));
})();
""" % (json.dumps(list_payload([row(857),
                                dict(row(858), title="T2-changed")])),
       json.dumps(changed)), setup)
        self.assertTrue(out[0]["focusedSecond"])
        self.assertTrue(out[0]["evKept"])
        self.assertTrue(out[0]["openKept"])
        self.assertTrue(out[0]["listRefetched"])
        self.assertTrue(out[0]["detailRefetched"])
        self.assertTrue(out[0]["newTitle"])

    def test_deeplink_selects_pr(self):
        setup = ("LIST = %s; NODE = %s;"
                 "pendingPrSel = {repo: 'r', num: '857'};" % (
                     json.dumps(list_payload([row(857)])),
                     json.dumps(node_payload())))
        out = run_driver("""
(async () => {
  await paintRepoPrs({name: "r"});
  await new Promise(r => setTimeout(r, 20));
  const html = $("tabbody").innerHTML;
  console.log(JSON.stringify({insp: html.includes('id="pr-inspector"'),
    sel: prSelectedNum("r")}));
})();
""", setup)
        self.assertTrue(out[0]["insp"])
        self.assertEqual(out[0]["sel"], "857")


def review_row(num, status, bot=False, title="T", stale=False,
                partial=False, fp=None):
    return {"number": num, "title": title, "author": "c", "bot": bot,
            "is_draft": False, "head": "fix", "closes": [344],
            "additions": 10, "deletions": 2, "files": 1,
            "created_at": "t", "updated_at": "t",
            "review": {"status": status, "stale": stale, "partial": partial,
                       "job_status": "idle", "job_error": None,
                       "content_fp": fp}}


def node_conv(num=857):
    node = node_payload(
        num=num, status="reviewed",
        report={"summary": "S", "findings": ["f"],
                "labels": {"status": ["needs-action"],
                           "platform": ["ios"], "type": ["bug"]},
                "evidence": [], "reply_draft": None,
                "source_files": ["src/a.ts"],
                "recommendation": "needs-changes",
                "recommendation_basis": "B"})
    # Unrelated section partial (CI unavailable) with a COMPLETE PR
    # description body: the BODY summary must stay plain while the
    # comment-section partial note stays visible. Old det.partial.length
    # code would wrongly badge the body here.
    node["snapshot"]["partial"] = ["synthetic preview: checks section unavailable"]
    node["snapshot"]["checks"] = []
    d = node["snapshot"]["detail"]
    d["comments"] = [{"id": "c1", "author": "anna",
                      "body": "일반 댓글 본문-AAA", "body_truncated": True,
                      "created_at": "t",
                      "url": "https://example.invalid/c1"}]
    d["reviews"] = [{"id": "r1", "author": "bob",
                     "state": "CHANGES_REQUESTED",
                     "body": "일반 리뷰 본문-BBB",
                     "url": "https://example.invalid/r1"}]
    d["comments_complete"] = False
    d["comments_note"] = "댓글 일부만 수집됨"
    return node


class TestPrRepair(unittest.TestCase):
    def test_unknown_error_counts_no_false_changed(self):
        setup = ("LIST = %s; NODE = %s;" % (
            json.dumps(list_payload(
                [row(857),
                 review_row(858, "unknown", stale=True, partial=True),
                 review_row(859, "error")])),
            json.dumps(node_payload())))
        out = run_driver("""
(async () => {
  await paintRepoPrs({name: "r"});
  const html = $("tabbody").innerHTML;
  console.log(JSON.stringify({
    fetch: html.includes("가져오기 필요 1건"),
    err: html.includes("오류 1건"),
    review: html.includes("미검토 1건"),
    noFalseChanged: !html.includes("·바뀜")}));
})();
""", setup)
        self.assertTrue(out[0]["fetch"])
        self.assertTrue(out[0]["err"])
        self.assertTrue(out[0]["review"])
        self.assertTrue(out[0]["noFalseChanged"])

    def test_first_use_known_missing_is_guidance(self):
        setup = ("LIST = %s; NODE = new Error("
                 "\"no stored PR snapshot yet; fetch it first\");" % (
                     json.dumps(list_payload([row(857)]))))
        out = run_driver("""
(async () => {
  await paintRepoPrs({name: "r"});
  const btns = $("tabbody").querySelectorAll("[data-prrowbtn]");
  await btns[0].click();
  await new Promise(r => setTimeout(r, 20));
  const html = $("tabbody").innerHTML;
  console.log(JSON.stringify({
    fetch: $("tabbody").querySelectorAll("[data-prfetch]").length,
    back: $("tabbody").querySelectorAll("[data-prback]").length,
    guidance: html.includes("원문 가져오기"),
    firstUse: html.includes("미가져옴"),
    noFailure: !html.includes("조회 오류")}));
})();
""", setup)
        self.assertEqual(out[0]["fetch"], 1)
        self.assertEqual(out[0]["back"], 1)
        self.assertTrue(out[0]["guidance"])
        self.assertTrue(out[0]["firstUse"])
        self.assertTrue(out[0]["noFailure"])

    def test_first_use_arbitrary_error_stays_failure(self):
        setup = ("LIST = %s; NODE = new Error(\"boom-502\");" % (
            json.dumps(list_payload([row(857)]))))
        out = run_driver("""
(async () => {
  await paintRepoPrs({name: "r"});
  const btns = $("tabbody").querySelectorAll("[data-prrowbtn]");
  await btns[0].click();
  await new Promise(r => setTimeout(r, 20));
  const html = $("tabbody").innerHTML;
  console.log(JSON.stringify({
    fetch: $("tabbody").querySelectorAll("[data-prfetch]").length,
    back: $("tabbody").querySelectorAll("[data-prback]").length,
    retry: html.includes("다시 가져오기"),
    errChip: html.includes("조회 오류"),
    errText: html.includes("상세 조회 오류: boom-502"),
    noFalseMissing: !html.includes("미가져옴") &&
      !html.includes("아직 이 PR의 원문을 가져오지 않았습니다")}));
})();
""", setup)
        self.assertEqual(out[0]["fetch"], 1)
        self.assertEqual(out[0]["back"], 1)
        self.assertTrue(out[0]["retry"])
        self.assertTrue(out[0]["errChip"])
        self.assertTrue(out[0]["errText"])
        self.assertTrue(out[0]["noFalseMissing"])

    def test_same_row_reselect_focuses_inspector(self):
        setup = ("LIST = %s; NODE = %s;" % (
            json.dumps(list_payload([row(857)])),
            json.dumps(node_payload())))
        out = run_driver("""
(async () => {
  await paintRepoPrs({name: "r"});
  let btns = $("tabbody").querySelectorAll("[data-prrowbtn]");
  await btns[0].click();
  await new Promise(r => setTimeout(r, 20));
  const first = $("tabbody")._focused === "#pr-inspector";
  // Park simulated focus back on the row: the second assertion must
  // observe a fresh focus transition, not a stale pointer.
  btns = $("tabbody").querySelectorAll("[data-prrowbtn]");
  $("tabbody")._focused = btns[0];
  document.activeElement = btns[0];
  await btns[0].click();
  await new Promise(r => setTimeout(r, 20));
  console.log(JSON.stringify({first,
    again: $("tabbody")._focused === "#pr-inspector"}));
})();
""", setup)
        self.assertTrue(out[0]["first"])
        self.assertTrue(out[0]["again"])

    def test_back_to_bot_opens_disclosure_and_focuses_row(self):
        setup = ("LIST = %s; NODE = %s;" % (
            json.dumps(list_payload(
                [row(857), review_row(1, "unknown", bot=True, stale=True,
                                      partial=True)])),
            json.dumps(node_payload(num=1))))
        out = run_driver("""
(async () => {
  await paintRepoPrs({name: "r"});
  const btns = $("tabbody").querySelectorAll("[data-prrowbtn]");
  await btns[1].click();
  await new Promise(r => setTimeout(r, 20));
  await $("tabbody").querySelectorAll("[data-prback]")[0].click();
  await new Promise(r => setTimeout(r, 20));
  const dk = $("tabbody").querySelectorAll("details[data-dk]");
  const bots = dk.find(d => d.dataset.dk === "pr-bots");
  const f = $("tabbody")._focused;
  console.log(JSON.stringify({
    botsOpen: !!(bots && bots.open),
    rowFocused: !!(f && f.dataset && f.dataset.prrowbtn === "1")}));
})();
""", setup)
        self.assertTrue(out[0]["botsOpen"])
        self.assertTrue(out[0]["rowFocused"])

    def test_conversation_partial_identity_labels_evidence_key(self):
        setup = ("LIST = %s; NODE = %s;" % (
            json.dumps(list_payload([row(857)])),
            json.dumps(node_conv())))
        out = run_driver("""
(async () => {
  await paintRepoPrs({name: "r"});
  const btns = $("tabbody").querySelectorAll("[data-prrowbtn]");
  await btns[0].click();
  await new Promise(r => setTimeout(r, 20));
  const html = $("tabbody").innerHTML;
  const inspHtml = html.slice(html.indexOf('id="pr-inspector"'));
  console.log(JSON.stringify({
    ident: inspHtml.includes("PR #857 "),
    comment: inspHtml.includes("일반 댓글 본문-AAA"),
    review: inspHtml.includes("일반 리뷰 본문-BBB"),
    count: inspHtml.includes("대화(댓글·리뷰) 2건"),
    partial: inspHtml.includes(">일부<") && inspHtml.includes("댓글 일부만 수집됨"),
    bodyPlain: inspHtml.includes("<summary>본문</summary>"),
    trunc: inspHtml.includes("(본문 잘림)"),
    rec: inspHtml.includes("수정 필요</b> (needs-changes)"),
    facet: inspHtml.includes("상태:조치 필요") &&
      inspHtml.includes('title="status:needs-action"'),
    facetKo: inspHtml.includes(">종류:버그<"),
    evkey: inspHtml.includes('data-k="pr-ev-857"'),
    appliedRaw: inspHtml.includes(">bug<")}));
})();
""", setup)
        for key in ("ident", "comment", "review", "count", "partial",
                    "trunc", "rec", "facet", "facetKo", "evkey",
                    "appliedRaw", "bodyPlain"):
            self.assertTrue(out[0][key], key)

    def test_body_truncated_flag_marks_body_only(self):
        node = node_conv()
        node["snapshot"]["detail"]["body_truncated"] = True
        setup = ("LIST = %s; NODE = %s;" % (
            json.dumps(list_payload([row(857)])),
            json.dumps(node)))
        out = run_driver("""
(async () => {
  await paintRepoPrs({name: "r"});
  const btns = $("tabbody").querySelectorAll("[data-prrowbtn]");
  await btns[0].click();
  await new Promise(r => setTimeout(r, 20));
  const html = $("tabbody").innerHTML;
  const inspHtml = html.slice(html.indexOf('id="pr-inspector"'));
  console.log(JSON.stringify({
    bodyBadge: inspHtml.includes("본문") && inspHtml.includes(">일부 잘림<")}));
})();
""", setup)
        self.assertTrue(out[0]["bodyBadge"])

    def test_real_capture_restore_focus_and_quiet_no_steal(self):
        # Real shipped captureReviewState through the real GET+render
        # path on a changed fingerprint, plus real async restore. The
        # browser drops focus when innerHTML is replaced; the driver
        # simulates that in the async gap between paint-start capture
        # and post-render restore (capture already ran synchronously).
        # Fake-DOM limits, stated here: no layout/scroll metrics and no
        # caret/selection API, so scroll-top/caret restoration is not
        # asserted; actual Chrome remains the layout/real-caret gate.
        setup = ("LIST = %s; NODE = %s;" % (
            json.dumps(list_payload([row(857)])),
            json.dumps(node_payload())))
        out = run_driver("""
(async () => {
  document.body = {};
  await paintRepoPrs({name: "r"});
  const btns = $("tabbody").querySelectorAll("[data-prrowbtn]");
  await btns[0].click();
  await new Promise(r => setTimeout(r, 20));
  const det = $("tabbody").querySelector("#pr-inspector")
    .querySelectorAll("details[data-dk]");
  det.find(d => d.dataset.dk === "pr-body").open = true;
  const inp = $("tabbody").querySelectorAll("[data-prevs]")[0];
  inp.value = "src/keep.ts";
  inp.oninput();
  document.activeElement = inp;
  const st = captureReviewState($("tabbody"));
  const keyOk = st.key === "pr-ev-857";
  const openOk = st.open.indexOf("pr-body") !== -1;
  // Changed fingerprint: real list GET + detail refetch + full render.
  const listGets0 = GETS.filter(u => u.startsWith("/api/prs?")).length;
  const detGets0 = GETS.filter(u => u.startsWith("/api/prs/detail")).length;
  LIST = %s;
  prFetchedAt = {};
  document.activeElement = inp;
  const p = paintRepoPrs({name: "r"});
  document.activeElement = document.body;
  await p;
  await new Promise(r => setTimeout(r, 40));
  const listGets1 = GETS.filter(u => u.startsWith("/api/prs?")).length;
  const detGets1 = GETS.filter(u => u.startsWith("/api/prs/detail")).length;
  const fresh = $("tabbody").querySelectorAll("[data-prevs]")[0];
  const det2 = $("tabbody").querySelector("#pr-inspector")
    .querySelectorAll("details[data-dk]");
  const changed = tabEl._focused === fresh &&
    det2.find(d => d.dataset.dk === "pr-body").open === true &&
    fresh.value === "src/keep.ts" &&
    listGets1 > listGets0 && detGets1 > detGets0;
  // Quiet poll while the user now reads elsewhere must not steal focus.
  document.activeElement = {dataset: {other: "1"}};
  const aeBefore = document.activeElement;
  const fBefore = tabEl._focused;
  await paintRepoPrs({name: "r"});
  await new Promise(r => setTimeout(r, 20));
  console.log(JSON.stringify({keyOk, openOk, changed,
    noSteal: tabEl._focused === fBefore &&
      document.activeElement === aeBefore}));
})();
""" % (json.dumps(list_payload([dict(row(857), title="T2-changed")]))),
            setup)
        for key in ("keyOk", "openOk", "changed", "noSteal"):
            self.assertTrue(out[0][key], key)


class TestPrErrorNodeRecovery(unittest.TestCase):
    def test_error_node_recovers_on_tuple_change_no_busy_loop(self):
        list2 = list_payload([dict(row(857), title="T2-changed")])
        setup = ("LIST = %s; NODE = new Error(\"boom-404\");" % (
            json.dumps(list_payload([row(857)]))))
        out = run_driver("""
(async () => {
  await paintRepoPrs({name: "r"});
  const btns = $("tabbody").querySelectorAll("[data-prrowbtn]");
  await btns[0].click();
  await new Promise(r => setTimeout(r, 30));
  const errShown = $("tabbody").innerHTML.includes("조회 오류");
  const detGets0 = GETS.filter(u => u.startsWith("/api/prs/detail")).length;
  // Upstream fetch happened elsewhere: new list tuple, node now loads.
  LIST = %s;
  NODE = %s;
  prFetchedAt = {};
  await paintRepoPrs({name: "r"});
  await new Promise(r => setTimeout(r, 30));
  const detGets1 = GETS.filter(u => u.startsWith("/api/prs/detail")).length;
  const html2 = $("tabbody").innerHTML;
  // Same quiet poll again: no further GET — recovery is one-shot.
  await paintRepoPrs({name: "r"});
  await new Promise(r => setTimeout(r, 30));
  const detGets2 = GETS.filter(u => u.startsWith("/api/prs/detail")).length;
  console.log(JSON.stringify({errShown,
    recovered: html2.includes("PR #857 ") && html2.includes("T2-changed"),
    refetchedOnce: detGets1 === detGets0 + 1,
    noLoop: detGets2 === detGets1}));
})();
""" % (json.dumps(list2), json.dumps(node_payload())), setup)
        for key in ("errShown", "recovered", "refetchedOnce", "noLoop"):
            self.assertTrue(out[0][key], key)

def stored_attempt(rid, outcome="reviewed", afp="b" * 64, report=None):
    """A genuine immutable attempt record, as written by _finish_job."""
    rep = report if report is not None else {
        "summary": "S", "findings": ["f"],
        "labels": {"status": ["needs-action"], "platform": ["ios"],
                   "type": ["bug"]},
        "evidence": [], "reply_draft": None, "source_files": [],
        "recommendation": "needs-info", "recommendation_basis": "B"}
    return {"repo": "o/r", "number": 857, "rid": rid, "outcome": outcome,
            "error": None if outcome == "reviewed" else "x",
            "semantic_fp": "s" * 64, "attempt_fp": afp,
            "head_sha": "a" * 40, "base_sha": "b" * 40,
            "requested_paths": ["src/a.ts"], "finished_at": "t",
            "usage": None, "input_snapshot": {}, "prompt": "p",
            "raw_output": "", "raw_output_meta": {}, "partial": [],
            "report": rep}


def node_from_real_api(review_rec, job_rec, snap, attempts=()):
    """Serialize actual api_prs_detail output as the loader payload.

    The UI must render what the real backend contract produces — a
    Source9-shaped reviewed state whose history is derived by the
    backend from an EMPTY history plus the finished job rid and the
    genuinely stored immutable attempt — not a hand-built richer
    object. Every attempt in `attempts` is stored as a real file, so
    validation exercises the actual validator, not a fixture.
    """
    import shutil
    from openmainty import pr_detail as _p
    from openmainty import pr_reviews as _r
    from openmainty import serve as _serve
    from openmainty.util import write_json as _wj
    from openmainty import repos as _repos
    tmp = tempfile.mkdtemp(prefix="pr91-ui-api-")
    prev = os.environ.get("OPENMAINTY_HOME")
    os.environ["OPENMAINTY_HOME"] = tmp
    try:
        fp = _r.content_fp(snap)
        _repos.save({"name": "r", "github": "o/r", "baseBranch": "main",
                     "knowledge": [], "checks": [], "e2e": [],
                     "release": {}, "announce": {}})
        _p.store_snapshot("o/r", snap)
        _wj(_p.review_path("o/r", 857), dict(review_rec,
                                            semantic_fp=fp))
        # A correctly stored done job (with kind): a raw kind-less
        # write is rejected by job_state before any derivation runs,
        # which would fake an unreachable legacy link.
        _r._write_job("o/r", 857, dict(job_rec, semantic_fp=fp))
        for att in attempts:
            _wj(_p.attempt_path("o/r", 857, att["rid"]), att)
        out = _serve.api_prs_detail({"repo": "r", "number": "857"})
        return json.loads(json.dumps(
            {"repo": "r", "slug": "o/r", "linked_runs": {"344": None},
             "snapshot": out["snapshot"], "review": out["review"],
             "model": {"ok": True, "reason": None}}))
    finally:
        if prev is None:
            os.environ.pop("OPENMAINTY_HOME", None)
        else:
            os.environ["OPENMAINTY_HOME"] = prev
        shutil.rmtree(tmp, ignore_errors=True)


def legacy_review_rec():
    return {"status": "reviewed", "attempt_fp": "a" * 64,
            "report": node_conv()["review"]["report"],
            "labels": {"status": ["needs-action"],
                       "platform": ["ios"], "type": ["bug"]},
            "partial": True, "history": []}


class TestPrHistoryAndRecovery(unittest.TestCase):
    def test_history_derives_legacy_attempt_from_empty_history(self):
        # Source9 shape: EMPTY recorded history, finished job, one
        # genuinely stored immutable attempt. The backend must derive
        # the reachable legacy rid — not require a prebuilt history.
        snap = dict(node_payload()["snapshot"])
        node = node_from_real_api(
            legacy_review_rec(),
            {"status": "done", "finished_at": "t", "rid": "rid-legacy",
             "attempt_fp": "a" * 64},
            snap, attempts=[stored_attempt("rid-legacy")])
        hist = node["review"]["history"]
        self.assertEqual([h.get("rid") for h in hist], ["rid-legacy"])
        self.assertTrue(hist[0].get("validated"))
        setup = ("LIST = %s; NODE = %s;" % (
            json.dumps(list_payload([row(857)])),
            json.dumps(node)))
        out = run_driver("""
(async () => {
  await paintRepoPrs({name: "r"});
  const btns = $("tabbody").querySelectorAll("[data-prrowbtn]");
  await btns[0].click();
  await new Promise(r => setTimeout(r, 30));
  const html = $("tabbody").innerHTML;
  const inspHtml = html.slice(html.indexOf('id="pr-inspector"'));
  console.log(JSON.stringify({
    attemptBtn: inspHtml.includes('data-pratt="857|rid-legacy"'),
    attemptText: inspHtml.includes("시도 보기"),
    histHead: inspHtml.includes("시도 기록")}));
})();
""", setup)
        for key in ("attemptBtn", "attemptText", "histHead"):
            self.assertTrue(out[0][key], key)

    def test_valid_failed_attempt_stays_inspectable(self):
        # Validated/readable is not reviewed/success: an honest failed
        # attempt keeps its inspect button, error text, and the top
        # latest-attempt link.
        snap = dict(node_payload()["snapshot"])
        err = stored_attempt("rid-err", outcome="error", afp="c" * 64)
        err["error"] = "synthetic model timeout"
        node = node_from_real_api(
            legacy_review_rec(),
            {"status": "done", "finished_at": "t", "rid": "rid-err",
             "attempt_fp": "c" * 64},
            snap, attempts=[err])
        hist = node["review"]["history"]
        self.assertEqual(len(hist), 1)
        self.assertTrue(hist[0].get("validated"))
        # The attempt viewer loads through the real loader contract:
        # ATTEMPT mirrors the actual api_prs_attempt envelope for the
        # same genuinely stored record (same rid/outcome/error), so the
        # click exercises fetch -> cache -> repaint, not injected DOM.
        setup = ("LIST = %s; NODE = %s; ATTEMPT = %s;" % (
            json.dumps(list_payload([row(857)])),
            json.dumps(node),
            json.dumps({"repo": "r", "slug": "o/r", "attempt": err})))
        out = run_driver("""
(async () => {
  await paintRepoPrs({name: "r"});
  const btns = $("tabbody").querySelectorAll("[data-prrowbtn]");
  await btns[0].click();
  await new Promise(r => setTimeout(r, 30));
  let html = $("tabbody").innerHTML;
  const inspHtml = html.slice(html.indexOf('id="pr-inspector"'));
  const atts = $("tabbody").querySelectorAll("[data-pratt]")
    .filter(b => b.dataset && b.dataset.pratt === "857|rid-err");
  if (!atts.length) throw new Error("no rid-err attempt link rendered");
  await atts[0].click();
  await new Promise(r => setTimeout(r, 40));
  html = $("tabbody").innerHTML;
  const viewIdx = html.indexOf("data-prattview=");
  const viewHtml = viewIdx >= 0 ? html.slice(viewIdx) : "";
  const gets = GETS.filter(u => u.startsWith("/api/prs/attempt")).length;
  console.log(JSON.stringify({
    errBtn: inspHtml.includes('data-pratt="857|rid-err"'),
    fetched: gets > 0,
    errText: viewHtml.includes("synthetic model timeout"),
    noHealthyRec: viewHtml.indexOf("권장:") < 0,
    latestBtn: inspHtml.includes("최신 시도 보기")}));
})();
""", setup)
        for key in ("errBtn", "fetched", "errText", "noHealthyRec",
                    "latestBtn"):
            self.assertTrue(out[0][key], key)

    def test_missing_artifact_is_error_row_without_button(self):
        # A history rid with no stored artifact is an explicit error
        # row, never a clickable healthy attempt.
        snap = dict(node_payload()["snapshot"])
        node = node_from_real_api(
            dict(legacy_review_rec(),
                 history=[{"at": "t", "event": "review-reviewed",
                           "rid": "rid-ghost"}]),
            {"status": "done", "finished_at": "t", "rid": "rid-ghost",
             "attempt_fp": "a" * 64},
            snap, attempts=[])
        hist = node["review"]["history"]
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0].get("event"), "attempt-unreadable")
        self.assertEqual(hist[0].get("rid"), "rid-ghost")
        setup = ("LIST = %s; NODE = %s;" % (
            json.dumps(list_payload([row(857)])),
            json.dumps(node)))
        out = run_driver("""
(async () => {
  await paintRepoPrs({name: "r"});
  const btns = $("tabbody").querySelectorAll("[data-prrowbtn]");
  await btns[0].click();
  await new Promise(r => setTimeout(r, 30));
  const html = $("tabbody").innerHTML;
  const inspHtml = html.slice(html.indexOf('id="pr-inspector"'));
  console.log(JSON.stringify({
    noGhostButton: !inspHtml.includes('data-pratt="857|rid-ghost"'),
    errorShown: inspHtml.includes("attempt-unreadable")}));
})();
""", setup)
        self.assertTrue(out[0]["noGhostButton"])
        self.assertTrue(out[0]["errorShown"])

    def _evidence_node(self, threads, checks):
        node = node_payload(
            status="reviewed",
            report={"summary": "S", "findings": ["f"],
                    "labels": {"status": ["needs-action"],
                               "platform": ["ios"], "type": ["bug"]},
                    "evidence": [
                        {"url": "https://github.com/o/r/pull/857"},
                        {"url": "https://github.com/o/r/pull/857"
                                "#discussion_r999"},
                        {"url": "https://ci.example/checks/ci-ios"}],
                    "reply_draft": None, "source_files": [],
                    "recommendation": "needs-info",
                    "recommendation_basis": "B"})
        node["snapshot"]["threads"] = threads
        node["snapshot"]["checks"] = checks
        return node

    def test_evidence_labels_resolve_against_collected_sources(self):
        # A correctly populated snapshot: labels prove actual
        # path/line/author/check names from collected data.
        node = self._evidence_node(
            threads=[{"path": "src/a.ts", "line": 12, "comments": [
                {"url": "https://github.com/o/r/pull/857"
                        "#discussion_r999",
                 "author": "coderabbitai", "body": "nit"}]}],
            checks=[{"name": "ci-ios",
                     "link": "https://ci.example/checks/ci-ios",
                     "status": "success"}])
        setup = ("LIST = %s; NODE = %s;" % (
            json.dumps(list_payload([row(857)])),
            json.dumps(node)))
        out = run_driver("""
(async () => {
  await paintRepoPrs({name: "r"});
  const btns = $("tabbody").querySelectorAll("[data-prrowbtn]");
  await btns[0].click();
  await new Promise(r => setTimeout(r, 30));
  const html = $("tabbody").innerHTML;
  const inspHtml = html.slice(html.indexOf('id="pr-inspector"'));
  console.log(JSON.stringify({
    prLink: inspHtml.includes("PR 원문 #857"),
    threadLink: inspHtml.includes("스레드 src/a.ts:12 · coderabbitai"),
    ciLink: inspHtml.includes("검사 ci-ios"),
    fullUrlKept: inspHtml.includes(
      'title="https://github.com/o/r/pull/857#discussion_r999"'),
    recFirst: inspHtml.indexOf("권장:") < inspHtml.indexOf("판단 근거"),
    koFacet: inspHtml.includes("플랫폼:iOS")}));
})();
""", setup)
        for key in ("prLink", "threadLink", "ciLink", "fullUrlKept",
                    "recFirst", "koFacet"):
            self.assertTrue(out[0][key], key)

    def test_evidence_labels_fall_back_without_collected_sources(self):
        # Absent metadata: honest fallbacks keep thread/CI identity
        # plus the exact URL, and never invent author/path names.
        node = self._evidence_node(threads=[], checks=[])
        setup = ("LIST = %s; NODE = %s;" % (
            json.dumps(list_payload([row(857)])),
            json.dumps(node)))
        out = run_driver("""
(async () => {
  await paintRepoPrs({name: "r"});
  const btns = $("tabbody").querySelectorAll("[data-prrowbtn]");
  await btns[0].click();
  await new Promise(r => setTimeout(r, 30));
  const html = $("tabbody").innerHTML;
  const inspHtml = html.slice(html.indexOf('id="pr-inspector"'));
  console.log(JSON.stringify({
    threadFallback: inspHtml.includes("스레드 r999"),
    checkFallback: inspHtml.includes("ci.example"),
    fullUrlKept: inspHtml.includes(
      'title="https://github.com/o/r/pull/857#discussion_r999"'),
    noInventedAuthor: !inspHtml.includes("coderabbitai")}));
})();
""", setup)
        for key in ("threadFallback", "checkFallback", "fullUrlKept",
                    "noInventedAuthor"):
            self.assertTrue(out[0][key], key)


if __name__ == "__main__":
    unittest.main()
