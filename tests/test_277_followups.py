"""Slice 277: gate-3 UI fixes L14-L17 (dashboard only).

L14: .wfield controls are 44px at every width (desktop was 40px).
L15: errors announce ONCE per transition into that error; a 30 s
  poll repainting the same error stays silent. role="alert" fires
  only when the error lead is new to its container (no global
  state, so unrelated repaints cannot leak announcements).
L16: the glance error gains an in-place 44px retry button plus an
  honest auto-retry line; the retry re-runs renderGlanceView once
  under its existing seq guard.
L17: the two known desk server reasons map to Korean; not-yet-built
  is a NEUTRAL empty state (no .inline-err, no alert) with retry;
  raw server strings stay inside a collapsed 44px "자세히".

Failing-first: every test below fails on the pre-277 tree (missing
helpers / 40px rule / missing retry) and passes after.
"""

import re
import subprocess
import unittest
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parent.parent / "src" \
    / "openmainty" / "dashboard.html"


def _script():
    text = DASHBOARD.read_text(encoding="utf-8")
    blocks = re.findall(r"<script>(.*?)</script>", text, re.S)
    assert len(blocks) == 1, "expected one inline script"
    return blocks[0]


def _style():
    text = DASHBOARD.read_text(encoding="utf-8")
    blocks = re.findall(r"<style>(.*?)</style>", text, re.S)
    assert len(blocks) == 1, "expected one inline style"
    return blocks[0]


def _balanced(src, i):
    depth, q, line, block, esc = 0, None, False, False, False
    for j in range(i, len(src)):
        c = src[j]
        n = src[j + 1] if j + 1 < len(src) else ""
        if line:
            if c == "\n":
                line = False
            continue
        if block:
            if c == "*" and n == "/":
                block = False
            continue
        if q:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == q:
                q = None
            continue
        if c == "/" and n == "/":
            line = True
            continue
        if c == "/" and n == "*":
            block = True
            continue
        if c in ("'", '"', "`"):
            q = c
            continue
        if c == "{":
            depth += 1
        if c == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    raise AssertionError("unbalanced braces")


def _decl(src, name):
    m = re.search(
        r"(?:^|\n)((?:async\s+)?function\s+" + re.escape(name)
        + r"\s*\([^)]*\))",
        src,
    )
    assert m, f"missing function {name}"
    return m.group(1) + " " + _balanced(src, src.index("{", m.start()))


def _decl_line(src, name):
    m = re.search(r"(?:^|\n)(function\s+" + re.escape(name)
                  + r"\s*\([^)]*\)\s*\{[^\n]*\})", src)
    assert m, f"missing one-line function {name}"
    return m.group(1)


def _const_decl(src, name):
    m = re.search(r"(?:^|\n)(const\s+" + re.escape(name)
                  + r"\s*=\s*[^\n;]+;)", src)
    assert m, f"missing const {name}"
    return m.group(1)


def _const_block(src, name):
    m = re.search(r"(?:^|\n)const\s+" + re.escape(name)
                  + r"\s*=\s*\{", src)
    assert m, f"missing const block {name}"
    start = src.index("{", m.start())
    return "const %s = %s;" % (name, _balanced(src, start))


def _run_node(script):
    return subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
    )


class L14FieldHeightCase(unittest.TestCase):
    def test_wfield_select_is_44_at_desktop(self):
        css = _style()
        m = re.search(r"\.wfield select \{([^}]*)\}", css)
        self.assertTrue(m, ".wfield select rule moved?")
        h = re.search(r"min-height:\s*(\d+)px", m.group(1))
        self.assertTrue(h, "no min-height on .wfield select")
        self.assertGreaterEqual(
            int(h.group(1)), 44,
            ".wfield select must be >=44px at desktop")

    def test_every_wfield_use_is_a_select(self):
        src = _script()
        labels = re.findall(
            r'<label class="wfield">(.*?)</label>', src, re.S)
        self.assertGreater(len(labels), 0, "no .wfield left?")
        for label in labels:
            self.assertIn("<select", label,
                          "non-select .wfield needs its own "
                          "44px check: %r" % label[:60])


L277_ALERT_STUBS = r'''
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a)); process.exitCode = 1; }
}
function ok(c, msg) { eq(!!c, true, msg); }
function stripTags(s) { return String(s).replace(/<[^>]*>/g, ""); }
// Minimal box: stores HTML, answers .inline-err queries.
function fakeBox() {
  return {_html: "",
    set innerHTML(v) { this._html = String(v); },
    get innerHTML() { return this._html; },
    querySelector(sel) {
      if (sel === "[data-glance-err]")
        return (this._html.indexOf("data-glance-err") !== -1) ? {} : null;
      if (sel === "[data-glance-err] .inline-err") {
        if (this._html.indexOf("data-glance-err") !== -1 &&
            this._html.indexOf("inline-err") !== -1)
          return {textContent: stripTags(this._html)};
        return null;
      }
      if (sel === ".inline-err" && this._html.indexOf("inline-err") !== -1)
        return {textContent: stripTags(this._html)};
      return null;
    }};
}
'''

L277_ALERT_CHECKS = r'''
// Fresh box: a new error alerts once.
var box = fakeBox();
var first = errBlockHtml("상태 요약", "boom", "", errNewToBox(box, fetchErrKo("상태 요약", "boom")));
ok(first.indexOf('role="alert"') !== -1, "alert-fresh");
box.innerHTML = first;
// Same-error repaint (the 30 s poll): silent.
var again = errBlockHtml("상태 요약", "boom", "", errNewToBox(box, fetchErrKo("상태 요약", "boom")));
ok(again.indexOf('role="alert"') === -1, "silent-repaint");
ok(again.indexOf("inline-err") !== -1, "repaint-keeps-style");
// A different LEAD (read -> not-found) is a new transition: alerts again.
// Two raw errors that map to the same lead are one error to the user.
var other = errBlockHtml("상태 요약", "boom (404)", "", errNewToBox(box, fetchErrKo("상태 요약", "boom (404)")));
ok(other.indexOf('role="alert"') !== -1, "alert-new-error");
// Healthy paint clears the habit: error after ok alerts.
box.innerHTML = "<div>ok</div>";
var back = errBlockHtml("상태 요약", "boom", "", errNewToBox(box, fetchErrKo("상태 요약", "boom")));
ok(back.indexOf('role="alert"') !== -1, "alert-after-ok");
// Default 3-arg callers are unchanged: no live region.
var plain = errBlockHtml("작업 상세", "raw boom", "<button>R</button>");
ok(plain.indexOf("role=") === -1, "default-silent");
ok(plain.indexOf("자세히") !== -1, "default-keeps-detail");
console.log("PASS-COUNT " + pass);
'''


class L15AnnounceCase(unittest.TestCase):
    def test_transition_alerts_once_poll_repaint_silent(self):
        src = _script()
        prog = (L277_ALERT_STUBS + _decl_line(src, "esc")
                + _const_block(src, "FETCH_ERR_TEXT")
                + _const_decl(src, "LOGIN_NEEDED_KO")
                + _const_decl(src, "APIKEY_SET_KO")
                + _const_decl(src, "SUB_APIKEY_RE")
                + _const_decl(src, "SUB_LOGIN_RE")
                + _decl(src, "subErrKo")
                + _decl(src, "fetchErrKo")
                + _decl(src, "errBlockHtml")
                + _decl(src, "errNewToBox")
                + L277_ALERT_CHECKS)
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)

    def test_glance_wires_transition_alert(self):
        body = _decl(_script(), "renderGlanceView")
        self.assertIn("errNewToBox(", body,
                      "renderGlanceView must pass a "
                      "transition-aware alert flag")

    def test_l18_foreign_error_counts_as_transition(self):
        # L18: a foreign .inline-err from another view must not
        # suppress the glance skeleton or the alert; only a
        # glance-owned (data-glance-err) error counts as "same".
        src = _script()
        prog = (L277_ALERT_STUBS
                + _decl(src, "errNewToBox")
                + r'''
var LEAD = "상태 요약을 읽는 중 문제가 생겼습니다";
// Foreign error, no glance marker: a transition even when the
// foreign text happens to contain the same lead words.
var fbox = fakeBox();
fbox.innerHTML = '<div class="inline-err">' + LEAD + ' (외부 화면)</div>';
ok(errNewToBox(fbox, LEAD, "[data-glance-err]") === true,
   "l18-foreign-is-transition");
// Same glance error on screen: silent poll repaint.
var gbox = fakeBox();
gbox.innerHTML = '<div data-glance-err="1"><div class="inline-err">' +
  LEAD + '</div></div>';
ok(errNewToBox(gbox, LEAD, "[data-glance-err]") === false,
   "l18-same-glance-error-silent");
// A changed glance lead still alerts.
ok(errNewToBox(gbox, "상태 요약의 주소를 찾지 못했습니다",
   "[data-glance-err]") === true, "l18-glance-lead-change-alerts");
// Default unscoped callers are unchanged.
ok(errNewToBox(fbox, LEAD + " (외부 화면)") === false,
   "l18-default-unchanged");
console.log("PASS-COUNT " + pass);
''')
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)

    def test_l18_glance_entry_uses_owned_marker(self):
        body = _decl(_script(), "renderGlanceView")
        # The skeleton decision and the alert decision both key
        # on the glance-owned marker, never a generic error.
        self.assertIn('querySelector("[data-glance-err]")', body)
        self.assertIn('data-glance-err="1"', body)
        self.assertIn('errNewToBox(box, fetchErrKo("상태 요약", gmsg),'
                      ' "[data-glance-err]")', body)
        self.assertNotIn('".inline-err"', body)

    def test_digest_error_announces_once_and_retries(self):
        # Slice 280 (L15b/L24b): the digest error now alerts on a
        # transition only and offers a retry, like the glance error.
        # The quiet poll keeps a shown error (no skeleton wipe) so the
        # sniff below can stay silent on an identical repeat.
        body = _decl(_script(), "renderDigestView")
        self.assertIn('errNewToBox(box, fetchErrKo("하루 요약", dgmsg),'
                      ' "[data-digest-err]")', body)
        self.assertIn('data-digest-err="1"', body)
        self.assertIn('id="digest-retry"', body)
        self.assertIn("다시 시도", body)
        self.assertIn("renderDigestView(date, true)", body)
        self.assertIn("digestFp(data, prev)", body)
        self.assertIn("digestFp(data, \"\")", body)

    def test_loading_keeps_polite_status(self):
        src = _script()
        body = _decl(src, "renderGlanceView")
        self.assertIn('role="status"', body)
        self.assertIn("상태 요약을 불러오는 중이에요", body)


class L16RetryCase(unittest.TestCase):
    def test_glance_error_has_retry_and_honest_note(self):
        body = _decl(_script(), "renderGlanceView")
        self.assertIn('id="glance-retry"', body)
        self.assertIn("다시 불러오기", body)
        self.assertIn("화면이 열려 있으면 30초마다 자동으로 다시 "
                      "시도합니다", body)
        self.assertIn("관제센터에서 보기", body)

    def test_retry_respects_seq_guard(self):
        body = _decl(_script(), "renderGlanceView")
        # The retry re-runs the guarded renderer (which bumps
        # seq on entry); stale in-flight responses still bail.
        # Stacking is impossible through the button: the retry
        # re-run repaints the skeleton synchronously, destroying
        # the clicked button before any second click can land.
        self.assertIn("glance-retry", body)
        self.assertIn("gRetry.onclick", body)
        self.assertIn("renderGlanceView(true)", body)
        self.assertIn("renderGlanceView.seq = "
                      "(renderGlanceView.seq || 0) + 1", body)
        self.assertIn("mySeq !== renderGlanceView.seq", body)
        # Quiet polls keep a shown GLANCE error (so the catch
        # can sniff it and stay silent); only a manual retry
        # re-skeletons. Foreign errors never suppress this.
        self.assertIn("fromRetry || "
                      "!box.querySelector(\"[data-glance-err]\")",
                      body)

    def test_retry_button_is_44px(self):
        css = _style()
        m = re.search(r"#view \.btn \{([^}]*)\}", css)
        self.assertTrue(m, "#view .btn rule moved?")
        h = re.search(r"min-height:\s*(\d+)px", m.group(1))
        self.assertTrue(h, "no min-height on #view .btn")
        self.assertGreaterEqual(int(h.group(1)), 44)


L277_DESK_STUBS = r'''
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a)); process.exitCode = 1; }
}
function ok(c, msg) { eq(!!c, true, msg); }
'''

L277_DESK_CHECKS = r'''
// Not-yet-built: NEUTRAL (no .inline-err, never an alert).
var n = deskUnknownLead("status summary not yet built");
eq(n.cls, "neutral", "empty-cls");
ok(n.lead.indexOf("아직 상태 요약이 없어요") !== -1, "empty-ko");
var nh = deskUnknownHtml("status summary not yet built", true);
ok(nh.indexOf("inline-err") === -1, "empty-no-err-style");
ok(nh.indexOf("role=") === -1, "empty-no-alert");
ok(nh.indexOf("다시 불러오기") !== -1, "empty-keeps-retry");
ok(nh.indexOf("자세히") !== -1, "empty-keeps-detail");
ok(nh.indexOf("status summary not yet built") !== -1, "empty-keeps-raw");
// Unreadable: error styling, Korean lead, raw in details.
var u = deskUnknownLead("summary snapshot unreadable (kept in place)");
eq(u.cls, "err", "unread-cls");
ok(u.lead.indexOf("상태 요약 파일을 읽지 못했어요") !== -1, "unread-ko");
var uh = deskUnknownHtml("summary snapshot unreadable (kept in place)", true);
ok(uh.indexOf("inline-err") !== -1, "unread-err-style");
ok(uh.indexOf('role="alert"') !== -1, "unread-alerts");
ok(uh.indexOf("자세히") !== -1, "unread-keeps-detail");
ok(uh.indexOf("summary snapshot unreadable (kept in place)") !== -1, "unread-keeps-raw");
// Unknown reason: Korean lead, raw in details.
var o = deskUnknownLead("weird server boom");
eq(o.cls, "err", "other-cls");
ok(o.lead.indexOf("상태 요약을 읽지 못했습니다") !== -1, "other-ko");
var oh = deskUnknownHtml("weird server boom", false);
ok(oh.indexOf("inline-err") !== -1, "other-err-style");
ok(oh.indexOf('role="alert"') === -1, "other-silent-flag");
ok(oh.indexOf("weird server boom") !== -1, "other-keeps-raw");
// Escaping: hostile raw text never breaks out.
var xh = deskUnknownHtml("<script>alert(1)</script>", true);
ok(xh.indexOf("<script>") === -1, "esc-script");
console.log("PASS-COUNT " + pass);
'''


class L17EmptyStateCase(unittest.TestCase):
    def test_reason_mapping_and_neutral_state(self):
        src = _script()
        prog = (L277_DESK_STUBS + _decl_line(src, "esc")
                + _const_decl(src, "DESK_EMPTY_REASON")
                + _const_decl(src, "DESK_UNREADABLE_REASON")
                + _decl(src, "deskUnknownLead")
                + _decl(src, "deskUnknownHtml")
                + L277_DESK_CHECKS)
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)

    def test_desk_paths_use_mapping(self):
        src = _script()
        for name in ("deskHeadInner", "deskSectionHtml",
                     "paintDeskInto", "deskPaneHtml"):
            self.assertIn(
                "deskUnknown", _decl(src, name),
                "%s must route unknown reasons through the "
                "mapper" % name)

    def test_server_messages_untouched(self):
        # decisions.py keeps the exact English reasons: tests
        # and logs depend on them.
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent
                               .parent / "src"))
        from openmainty import decisions
        import inspect
        snap_src = inspect.getsource(decisions._snapshot)
        self.assertIn('"status summary not yet built"',
                      snap_src)
        self.assertIn('"summary snapshot unreadable (kept in '
                      'place)"', snap_src)
