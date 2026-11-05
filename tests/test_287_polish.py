"""Slice 287: digest redesign + F1-F7 polish (staging only).

Populated digest renders labelled groups as real lists (no dash/dot
chains, no raw '- ' prefixes). F1 statusChip uses visually-hidden full
text; F2 ledger rows are ul/li; F3 drops the lone 'evidence' heading;
F4 offline text is dot-free; F5 desk rows split title/body; F6 empty
digest gains an h1; F7 thin pill-row scrollbars.
"""
import re
import subprocess
import unittest
from pathlib import Path

NODE = "node"


def _src():
    base = Path(__file__).resolve().parent.parent
    return (base / "src" / "openmainty" / "dashboard.html").read_text(
        encoding="utf-8")


def _decl(src, name):
    for prefix in ("async function " + name + "(",
                   "function " + name + "("):
        i = src.find(prefix)
        if i != -1:
            break
    else:
        raise AssertionError("missing function " + name)
    j = src.index("{", i)
    depth = 0
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    raise AssertionError("unbalanced " + name)


def _const(src, name):
    m = re.search(r"const " + name + r"\s*=\s*\{.*?\};", src, re.S)
    assert m, "missing const " + name
    return m.group(0)


def _run_node(prog):
    return subprocess.run(
        [NODE, "-e", prog], capture_output=True, text=True, timeout=60)


def _node_ok(prog):
    cp = _run_node(prog)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    return cp.stdout


OK_STUB = r'''
var pass = 0;
function ok(c, name) {
  if (!c) { console.log("FAIL " + name); process.exitCode = 1; }
  else pass++;
}
'''


class DigestHeadTest(unittest.TestCase):
    def test_h1_and_asof(self):
        src = _src()
        prog = OK_STUB + _decl(src, "esc") + _decl(src, "fmtKST") + \
            _decl(src, "fmtKSThm") + _decl(src, "digestHeadHtml") + r'''
var h = digestHeadHtml({title: "2026-11-04 하루 요약",
  date: "2026-11-04", snapshot_at: "2026-11-04T09:55:00Z",
  at: "2026-11-04T10:00:00Z"});
ok(h.indexOf("<h1>2026-11-04 하루 요약</h1>") !== -1, "h1");
ok(h.indexOf("자료 시각") !== -1, "asof-label");
ok(h.indexOf("KST") !== -1, "asof-zone");
var h2 = digestHeadHtml({date: "2026-11-04"});
ok(h2.indexOf("<h1>하루 요약 (2026-11-04)</h1>") !== -1, "fallback");
ok(h2.indexOf("자료 시각") === -1, "no-asof");
console.log("pass=" + pass);
'''
        out = _node_ok(prog)
        self.assertIn("pass=5", out)


class DigestRepoTest(unittest.TestCase):
    def _prog(self, extra):
        src = _src()
        return (OK_STUB + _decl(src, "esc") + _const(src, "M1_VERDICT_CLS")
                + _const(src, "M1_VERDICT_KO")
                + _const(src, "DG_NEED_KO")
                + _decl(src, "digestNeedHtml")
                + _decl(src, "digestCountsHtml")
                + _decl(src, "digestRepoHtml") + "\n" + extra)

    def test_groups_are_real_lists(self):
        prog = self._prog(r'''
var c = {repo: "demo-sample-repo", verdict: "attention",
  verdict_line: "주의 — 검증된 업데이트 1건이 출시 대기 중, " +
    "검토 대기 0건, 이슈 검토 대기 0건, 막힌 것 1, " +
    "당신의 답이 필요한 질문 1건",
  released: {count: 1, release_tag: "v2",
    release_tag_date: "2026-10-01", last_release: "2026-10-01",
    days_since: 34, basis: "tag"},
  story: ["expo 57.0.11 검증 끝남"],
  need_you: [{kind: "question", ref: "run-1",
    question: "#7 제목 — 어느 버전으로?", age_text: "3일째 대기"}],
  blocked: [{what: "w", reason: "r"}]};
var h = digestRepoHtml(c);
ok(h.indexOf("<h2>") !== -1, "h2");
ok(h.indexOf("demo-sample-repo") !== -1, "repo");
ok(h.indexOf("주의") !== -1, "chip");
ok(h.indexOf("<ul") !== -1, "ul");
ok(h.indexOf("<li>") !== -1, "li");
ok(h.indexOf("· expo") === -1, "no-dot-prefix");
ok(h.indexOf("- expo") === -1, "no-dash-prefix");
ok(h.indexOf("질문(3일째 대기)") === -1, "no-q-chain");
ok(h.indexOf("막힘: w") === -1, "no-blocked-chain");
ok(h.indexOf("출시 대기") !== -1, "released-group");
ok(h.indexOf("#/work/run-1") !== -1, "run-link");
ok(h.indexOf("3일째 대기") !== -1, "age");
ok(h.indexOf("dg-counts") !== -1, "fields");
ok(h.indexOf("출시 대기 <b>1</b>") !== -1, "field-rel");
ok(h.indexOf("막힘 <b>1</b>") !== -1, "field-blocked");
ok(h.indexOf("답변 필요 <b>1</b>") !== -1, "field-q");
ok(h.indexOf('title="주의 — 검증된 업데이트 1건이 출시 대기 중, 검토 대기 0건') !== -1, "full-title");
var vis = h.replace(/<span class="vh">.*?<\/span>/g, "|").replace(/<[^>]*>/g, "|");
ok(vis.indexOf("주의 —") === -1, "no-state-prefix");
ok(h.indexOf("답변 대기 질문 1건") !== -1, "q-heading");
ok(h.indexOf("dg-lead") === -1, "no-lead-div");
ok(h.indexOf("dg-rel-tag") !== -1, "rel-tag");
ok(h.indexOf("검증된 업데이트 1건 출시 대기") !== -1, "rel-main");
ok(h.indexOf("마지막 출시 v2") !== -1, "rel-last");
ok(h.indexOf("2026-10-01 (34일 전)") !== -1, "rel-date");
ok(h.indexOf("예정") === -1, "no-future-word");
ok(vis.indexOf("검토 대기") === -1, "zero-omitted");
console.log("pass=" + pass);
''')
        out = _node_ok(prog)
        self.assertIn("pass=26", out)

    def test_quiet_when_nothing(self):
        prog = self._prog(r'''
var h = digestRepoHtml({repo: "r", verdict: "normal",
  verdict_line: "정상 — 새로 들어온 업데이트 없음, 막힌 것 0",
  released: {count: 0}, story: [], need_you: [], blocked: []});
ok(h.indexOf("변화 없음") !== -1, "quiet");
ok(h.indexOf("<ul>") === -1, "no-lists");
ok(h.indexOf("정상 — 새로") === -1, "no-echo");
var u = digestRepoHtml({repo: "r", verdict: "unknown",
  verdict_line: "미확인 — 개요 읽기 실패해 판단 보류",
  story: [], need_you: [], blocked: []});
ok(u.indexOf("개요 읽기 실패해 판단 보류") !== -1, "unknown-kept");
console.log("pass=" + pass);
''')
        out = _node_ok(prog)
        self.assertIn("pass=4", out)

    def test_mixed_need_splits_groups(self):
        prog = self._prog(r'''
var c = {repo: "r", verdict: "attention",
  verdict_line: "주의 — 검증된 업데이트 0건이 출시 대기 중, " +
    "검토 대기 0건, 이슈 검토 대기 1건, 막힌 것 0, " +
    "당신의 답이 필요한 질문 2건",
  need_you: [{kind: "question", question: "Q1", age_text: "오늘"},
    {kind: "question", question: "Q2", age_text: "오늘"},
    {kind: "issue-review", title: "이슈 #12 분류 검토",
      age_text: "오늘"}],
  blocked: []};
var h = digestRepoHtml(c);
ok(h.indexOf("답변 대기 질문 2건") !== -1, "q-split");
ok(h.indexOf("판단이 필요한 일 1건") !== -1, "rest-split");
ok(h.indexOf("답변 대기 질문 3건") === -1, "no-mixed-count");
ok(h.indexOf("답변 필요 <b>2</b>") !== -1, "field-q2");
ok(h.indexOf("이슈 검토 <b>1</b>") !== -1, "field-rev");
console.log("pass=" + pass);
''')
        out = _node_ok(prog)
        self.assertIn("pass=5", out)

    def test_release_row_and_empty_body(self):
        prog = self._prog(r'''
var c = {repo: "r", verdict: "attention",
  verdict_line: "주의 — 검증된 업데이트 1건이 출시 대기 중, " +
    "검토 대기 0건, 이슈 검토 대기 0건, 막힌 것 1, " +
    "당신의 답이 필요한 질문 0건",
  released: {count: 1, release_tag: "v3.0.2",
    release_tag_date: "2026-09-21", last_release: "2026-09-21",
    days_since: 44, basis: "tag"},
  blocked: [{what: "막힌 일"}], story: [], need_you: []};
var h = digestRepoHtml(c);
ok(h.indexOf("검증된 업데이트 1건 출시 대기") !== -1, "rel-main");
ok(h.indexOf("마지막 출시 v3.0.2") !== -1, "rel-last");
ok(h.indexOf("2026-09-21 (44일 전)") !== -1, "rel-date");
ok(h.indexOf("예정") === -1, "no-future-word");
ok(h.indexOf("dg-need-b") === -1, "no-empty-body");
ok(h.indexOf("출시 대기 <b>1</b>") !== -1, "count-from-line");
var e = digestRepoHtml({repo: "r", verdict: "attention",
  verdict_line: "주의 — 검증된 업데이트 2건이 출시 대기 중, " +
    "검토 대기 0건, 이슈 검토 대기 0건, 막힌 것 0, " +
    "당신의 답이 필요한 질문 0건",
  released: {count: 2, release_tag: "",
    release_tag_date: null, last_release: null, days_since: null,
    basis: "unknown"},
  blocked: [], story: [], need_you: []});
ok(e.indexOf("출시 기록 없음") !== -1, "no-tag");
ok(e.indexOf("예정") === -1, "no-future-2");
var u = digestRepoHtml({repo: "r", verdict: "attention",
  verdict_line: "주의 — 검증된 업데이트 1건이 출시 대기 중, " +
    "검토 대기 0건, 이슈 검토 대기 0건, 막힌 것 0, " +
    "당신의 답이 필요한 질문 0건",
  released: {count: 1, release_tag: "v9",
    release_tag_date: null, last_release: null, basis: "unknown"},
  blocked: [], story: [], need_you: []});
ok(u.indexOf("마지막 출시 v9") !== -1, "tag-kept");
ok(u.indexOf("마지막 출시 미확인") !== -1, "date-unknown");
console.log("pass=" + pass);
''')
        out = _node_ok(prog)
        self.assertIn("pass=10", out)


class DigestCountsTest(unittest.TestCase):
    def test_parse_from_sentence(self):
        src = _src()
        prog = OK_STUB + _decl(src, "esc") + \
            _decl(src, "digestCountsHtml") + r'''
var h = digestCountsHtml("주의 — 검증된 업데이트 33건이 출시 대기 중, " +
  "검토 대기 39건, 이슈 검토 대기 2건, 막힌 것 0, " +
  "당신의 답이 필요한 질문 9건");
ok(h.indexOf("출시 대기 <b>33</b>") !== -1, "c-rel");
ok(h.indexOf("검토 대기 <b>39</b>") !== -1, "c-chk");
ok(h.indexOf("이슈 검토 <b>2</b>") !== -1, "c-rev");
var hvis = h.replace(/<span class="vh">.*?<\/span>/g, "|").replace(/<[^>]*>/g, "|");
ok(hvis.indexOf("막힘") === -1, "c-zero");
ok(h.indexOf("답변 필요 <b>9</b>") !== -1, "c-q");
ok(h.indexOf("dg-counts") !== -1, "c-row");
ok(h.indexOf('<span class="vh">주의 — 검증된 업데이트 33건') !== -1,
  "c-at");
var f = digestCountsHtml("미확인 — 개요 읽기 실패해 판단 보류");
ok(f.indexOf("dg-lead") !== -1, "c-fallback");
ok(f.indexOf("개요 읽기 실패해 판단 보류") !== -1, "c-fallback-text");
var g = digestCountsHtml("주의 — 검증된 업데이트 1건이 출시 대기 중, " +
  "알 수 없는 부분");
ok(g.indexOf("dg-lead") !== -1, "c-part-fallback");
ok(g.indexOf("dg-counts") === -1, "c-no-row");
console.log("pass=" + pass);
'''
        out = _node_ok(prog)
        self.assertIn("pass=11", out)


class DigestNeedTest(unittest.TestCase):
    def test_split_and_link(self):
        src = _src()
        prog = OK_STUB + _decl(src, "esc") + _const(src, "DG_NEED_KO") + \
            _decl(src, "digestNeedHtml") + r'''
var h = digestNeedHtml({kind: "question", ref: "run-9",
  question: "#431 분석 — 어느 버전으로?", age_text: "오늘"});
ok(h.indexOf('href="#/work/run-9"') !== -1, "link");
ok(h.indexOf("#431 분석") !== -1, "head");
ok(h.indexOf("어느 버전으로?") !== -1, "tail");
ok(h.indexOf("dg-need-t") !== -1, "clamp");
ok(h.indexOf("오늘") !== -1, "age");
var p = digestNeedHtml({kind: "question",
  question: "연결 없는 질문", age_text: "오늘"});
ok(p.indexOf("href=") === -1, "no-link");
var ir = digestNeedHtml({kind: "issue-review", title: "살펴보기",
  age_text: "1일째 대기"});
ok(ir.indexOf("이슈 검토") !== -1, "kind-label");
ok(ir.indexOf("살펴보기") !== -1, "title");
console.log("pass=" + pass);
'''
        out = _node_ok(prog)
        self.assertIn("pass=8", out)


class DigestEmptyTest(unittest.TestCase):
    def test_h1_and_fact(self):
        src = _src()
        prog = OK_STUB + _decl(src, "esc") + \
            _decl(src, "digestEmptyHtml") + r'''
var h = digestEmptyHtml("2026-11-04", "2026-11-03", "");
ok(h.indexOf("<h1>하루 요약</h1>") !== -1, "h1");
ok(h.indexOf("아직 2026-11-04 요약이 없습니다") !== -1, "fact");
ok(h.indexOf("digest-prev") !== -1, "prev");
ok(h.indexOf("한눈에 보기로") !== -1, "glance");
var n = digestEmptyHtml("오늘", "", "");
ok(n.indexOf("digest-prev") === -1, "no-prev");
console.log("pass=" + pass);
'''
        out = _node_ok(prog)
        self.assertIn("pass=5", out)


class DigestWiringTest(unittest.TestCase):
    def test_view_calls_helpers(self):
        src = _src()
        body = _decl(src, "renderDigestView")
        self.assertIn("digestHeadHtml(", body)
        self.assertIn(".map(digestRepoHtml)", body)
        self.assertIn("digestEmptyHtml(", body)
        self.assertIn("focusViewHeading()", body)
        self.assertIn('data-digestdate="', body)


class StatusChipTest(unittest.TestCase):
    def test_vh_full_text(self):
        src = _src()
        prog = OK_STUB + _decl(src, "esc") + \
            _decl(src, "statusChip") + r'''
var h = statusChip("결정 3건", "3");
ok(h.indexOf('title="결정 3건"') !== -1, "title");
ok(h.indexOf("aria-label") === -1, "no-aria-label");
ok(h.indexOf('class="vh"') !== -1, "vh");
ok(h.indexOf('<span class="vh">결정 3건</span>') !== -1, "vh-text");
ok(h.indexOf('aria-hidden="true">3<') !== -1, "hidden-short");
var x = statusChip('a"b', "c");
ok(x.indexOf("&quot;") !== -1, "escaped");
console.log("pass=" + pass);
'''
        out = _node_ok(prog)
        self.assertIn("pass=6", out)


class LedgerListTest(unittest.TestCase):
    def test_rows_are_list_items(self):
        src = _src()
        prog = OK_STUB + _decl(src, "esc") + _decl(src, "statusChip") + \
            _decl(src, "deskHeroHtml") + _decl(src, "deskLedgerHtml") + r'''
var h = deskLedgerHtml({supported: [{id: "a", repo: "r1",
  title_excerpt: "T", age_text: "오늘"}], repos: []});
ok(h.indexOf("<ul") !== -1, "ul");
ok(h.indexOf('<li class="ledger-row"') !== -1, "li");
ok(h.indexOf('<div class="ledger-row"') === -1, "no-div-row");
ok(h.indexOf("읽기 전용 판단 1건") !== -1, "count");
console.log("pass=" + pass);
'''
        out = _node_ok(prog)
        self.assertIn("pass=4", out)


class DeskEvidenceTest(unittest.TestCase):
    def test_no_lone_evidence_heading(self):
        src = _src()
        self.assertNotIn("정확한 근거", src)
        self.assertIn('data-deskgo="primary"', src)


class NetdotTest(unittest.TestCase):
    def test_offline_dot_free(self):
        src = _src()
        self.assertIn("오프라인, 다시 연결하는 중", src)
        self.assertNotIn("오프라인 · 재연결 중", src)


class DeskRowSplitTest(unittest.TestCase):
    def test_title_body_split(self):
        src = _src()
        prog = OK_STUB + _decl(src, "esc") + _decl(src, "deskKindKo") + \
            _decl(src, "deskRowHtml") + r'''
var h = deskRowHtml({id: "a", repo: "r", kind: "question",
  title_excerpt: "#7 데모 문구 — 데모 위젯 설명", age_text: "오늘"},
  false);
ok(h.indexOf("rowtitle") !== -1, "title");
ok(h.indexOf("#7 데모 문구") !== -1, "head");
var ti = h.indexOf("rowtitle");
var hi = h.indexOf("#7 데모 문구");
ok(hi > ti, "head-in-title");
ok(h.indexOf("rowbody") !== -1, "body-line");
ok(h.indexOf("데모 위젯 설명") !== -1, "tail");
var p = deskRowHtml({id: "b", repo: "r", kind: "question",
  title_excerpt: "짧은 제목", age_text: "오늘"}, false);
ok(p.indexOf("rowbody") === -1, "no-body");
console.log("pass=" + pass);
'''
        out = _node_ok(prog)
        self.assertIn("pass=6", out)


class PillScrollCssTest(unittest.TestCase):
    def test_thin_scrollbar_and_vh(self):
        src = _src()
        m = re.search(r"<style>(.*?)</style>", src, re.S)
        self.assertIsNotNone(m)
        css = m.group(1)
        self.assertIn("scrollbar-width: thin", css)
        self.assertIn(".dd-filter::-webkit-scrollbar", css)
        self.assertIn(".vh {", css)
        self.assertIn("dg-need-t", css)
        self.assertIn("rowbody", css)
        self.assertIn("a.dg-need-link:hover .dg-need-t", css)
        self.assertIn("a.dg-need-link:focus-visible .dg-need-t", css)
        self.assertIn(".dg-rel-tag", css)
        self.assertIn(".dg-rel-when", css)
        self.assertIn(".dg-counts", css)
        self.assertIn(".dg-fld + .dg-fld", css)


if __name__ == "__main__":
    unittest.main()
