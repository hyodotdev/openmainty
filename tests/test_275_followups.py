"""Slice 275: honest blocked-isolation-check UX on the Work view.

Server: describe_preflight reports a machine field blocked_by plus a
Korean-first reason when the latest terminal record is structural
(config/auth/native/key/contributor/policy); non-structural refusals
keep today's text. UI: blocked summary suffix, Korean-first panel
lines with the verbatim original, honest run/create wording, and a
role=note line by the start button whenever authority is not true.
The panel mirrors the server gate; nothing disables or gates
client-side.

Isolated temp homes only; never the real home. No deletes anywhere:
TemporaryDirectory cleanup only.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from openmainty import preflight as pf  # noqa: E402
from test_preflight import (  # noqa: E402
    _fake_transport, _fixture_observer, _fixture_values)

STRUCTURAL = ("config-unestablished", "auth-unestablished",
              "native-containment-unproved", "api-key-present",
              "contributor-env-present", "bad-policy")

ORIGINALS = {
    "config-unestablished":
        "preflight config surface is unestablished: no config "
        "identity to match, so target-A stays blocked.",
    "auth-unestablished":
        "no verified preflight credential observed on this host.",
    "native-containment-unproved":
        "native containment is unproved for the worker child tree.",
    "api-key-present":
        "API key present: refusing subscription-claimed run.",
    "contributor-env-present":
        "contributor-mode env var fights the pinned request.",
    "bad-policy":
        "request is not the dedicated source-repair profile.",
}


class DescribeGoldensCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pf275-")
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(self.home, mode=0o700)
        for var in ("META_API_KEY", "MUSE_CONTRIBUTOR_MODE",
                    "OPENMAINTY_MUSE_BINARY"):
            prev = os.environ.pop(var, None)
            if prev is not None:
                self.addCleanup(os.environ.__setitem__, var,
                                prev)

    def _world_with_refusal(self, code, reason, home=None):
        """Refused receipt with the given terminal code, via run."""
        home = home or self.home
        values = _fixture_values()
        values["config"] = {"status": "unestablished",
                            "files": [], "note": ""}
        with mock.patch("openmainty.preflight.current_identity",
                        return_value=values):
            created = pf.create_preflight(home,
                                          "golden-" + code)
            out = pf.run_preflight(
                home, created["pfid"],
                transport=_fake_transport(),
                observer=_fixture_observer(
                    auth_source="unknown"))
        self.assertEqual(out["code"], "config-unestablished")
        pfid = created["pfid"]
        directory = os.path.join(home, "preflight")
        outcome_path = os.path.join(
            directory, f"{pfid}.outcome.json")
        with open(outcome_path, "rb") as fh:
            outcome = json.loads(fh.read().decode("utf-8"))
        outcome["code"] = code
        outcome["reason"] = reason
        with open(outcome_path, "wb") as fh:
            fh.write(json.dumps(
                outcome, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False).encode("utf-8"))
        return pfid

    def test_structural_codes_honest_reason(self):
        for code in STRUCTURAL:
            with self.subTest(code=code):
                home = os.path.join(self.tmp.name, code)
                os.makedirs(home, mode=0o700)
                self._world_with_refusal(
                    code, ORIGINALS[code], home)
                got = pf.describe_preflight(home)
                self.assertEqual(got["state"], "not-verified",
                                 code)
                self.assertEqual(got["code"], "no-preflight",
                                 code)
                self.assertEqual(got["blocked_by"], code, code)
                self.assertNotIn("다시 실행", got["reason"], code)
                self.assertIn(code, got["reason"], code)
                self.assertFalse(
                    got["authority_now"]["authorized"], code)

    def test_non_structural_keeps_today_text(self):
        for code in ("channel-unknown", "probe-failed"):
            with self.subTest(code=code):
                home = os.path.join(self.tmp.name, code)
                os.makedirs(home, mode=0o700)
                self._world_with_refusal(
                    code, "probe reached an "
                          "unproved channel state.", home)
                got = pf.describe_preflight(home)
                self.assertEqual(got["state"], "not-verified",
                                 code)
                self.assertEqual(got["code"], "no-preflight",
                                 code)
                self.assertIn("blocked_by", got)
                self.assertIsNone(got["blocked_by"], code)
                self.assertIn("다시 실행하세요", got["reason"],
                              code)

    def test_created_only_has_no_blocked_by(self):
        values = _fixture_values()
        with mock.patch("openmainty.preflight.current_identity",
                        return_value=values):
            pf.create_preflight(self.home, "only-created")
        got = pf.describe_preflight(self.home)
        self.assertEqual(got["state"], "not-verified")
        self.assertIn("blocked_by", got)
        self.assertIsNone(got["blocked_by"])

    def test_legacy_shape_still_projects(self):
        pfid = self._world_with_refusal(
            "config-unestablished", ORIGINALS[
                "config-unestablished"])
        directory = os.path.join(self.home, "preflight")
        for kind in ("input", "outcome"):
            path = os.path.join(
                directory, f"{pfid}.{kind}.json")
            with open(path, "rb") as fh:
                doc = json.loads(fh.read().decode("utf-8"))
            key = ("auth_observed" if kind == "input"
                   else "auth")
            del doc[key]["contributor_env_present"]
            with open(path, "wb") as fh:
                fh.write(json.dumps(
                    doc, sort_keys=True, separators=(",", ":"),
                    ensure_ascii=False).encode("utf-8"))
        got = pf.describe_preflight(self.home)
        self.assertEqual(got["state"], "not-verified")
        self.assertEqual(got["code"], "no-preflight")
        self.assertEqual(got["blocked_by"],
                         "config-unestablished")
        self.assertEqual(len(got["history"]), 1)
        entry = got["history"][0]
        self.assertEqual(entry["state"], "refused")
        self.assertEqual(entry["code"], "config-unestablished")
        self.assertEqual(entry["reason"],
                         ORIGINALS["config-unestablished"])

    def test_blocked_by_key_present_all_states(self):
        self.assertIn("blocked_by",
                      pf.describe_preflight(self.home))


def _script():
    root = Path(__file__).resolve().parent.parent
    text = (root / "src" / "openmainty"
            / "dashboard.html").read_text()
    blocks = re.findall(r"<script>(.*?)</script>", text, re.S)
    assert len(blocks) == 1, "expected one inline script"
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
    # Single-line top-level function (esc): match the whole line.
    # _balanced cannot be used here: the /[&<>"]/ regex literal
    # would fool its quote tracking and swallow the file.
    m = re.search(r"(?:^|\n)(function\s+" + re.escape(name)
                  + r"\s*\([^)]*\)\s*\{[^\n]*\})", src)
    assert m, f"missing one-line function {name}"
    return m.group(1)


def _run_node(script):
    return subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
    )


L275_UI_STUBS = r'''
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a)); process.exitCode = 1; }
}
function ok(c, msg) { eq(!!c, true, msg); }
'''

L275_UI_CHECKS = r'''
var STRUCT = ["config-unestablished", "auth-unestablished",
  "native-containment-unproved", "api-key-present",
  "contributor-env-present", "bad-policy"];
STRUCT.forEach(function (code) {
  var short = pfBlockedShort(code);
  ok(short && short.length > 0, "short." + code);
  ok(short.indexOf("원문") === -1, "short-no-orig." + code);
});
eq(pfBlockedShort("channel-unknown"), "", "short-nonstrukt");
eq(pfBlockedShort(""), "", "short-empty");

var pfB = {state: "not-verified", code: "no-preflight",
  blocked_by: "config-unestablished",
  reason: "KOREAN SERVER REASON config-unestablished",
  history: [{pfid: "pf-1", state: "refused",
    code: "config-unestablished", reason: "ENGLISH ORIGINAL"}],
  authority_now: {authorized: false, code: "no-preflight",
    reason: "no verified preflight"}};
var title = pfSummaryTitle(pfB);
ok(title.indexOf("격리 점검 — 차단됨") === 0, "title-prefix");
ok(title.indexOf("설정 표면 미확립") !== -1, "title-suffix");
ok(title.indexOf("구독 인증·설정 증명 전까지 대기") !== -1,
  "title-suffix-tail");
var titlePlain = pfSummaryTitle({state: "not-verified",
  code: "no-preflight", blocked_by: null, reason: "x",
  history: []});
eq(titlePlain, "격리 점검 — 차단됨", "title-plain");

var lines = pfLeadHtml(pfB);
ok(lines.lead.indexOf("KOREAN SERVER REASON") !== -1,
  "lead-structural");
ok(lines.orig.indexOf("원문") !== -1, "orig-label");
ok(lines.orig.indexOf("ENGLISH ORIGINAL") !== -1,
  "orig{verbatim");
var pfN = {state: "not-verified", code: "no-preflight",
  blocked_by: null,
  reason: "latest isolation check is not verified: target stays blocked.",
  history: [{pfid: "pf-2", state: "refused",
    code: "channel-unknown", reason: "channel row unproved"}]};
var linesN = pfLeadHtml(pfN);
ok(linesN.lead.indexOf("미검증") !== -1, "lead-generic-ko");
ok(linesN.lead.indexOf("latest isolation") === -1,
  "lead-no-english");
ok(linesN.orig.indexOf("latest isolation check is not verified")
  !== -1, "orig-server-verbatim");

var runB = pfRunBlock(pfB, {pfid: "pf-9", state: "created"});
ok(runB.indexOf("모델 호출 없이 거부") !== -1, "run-honest");
ok(runB.indexOf("할당량 소모") === -1, "run-no-quota");
ok(runB.indexOf("pf-9") !== -1, "run-pfid");
var runN = pfRunBlock(pfN, {pfid: "pf-9", state: "created"});
ok(runN.indexOf("모델 1회") !== -1, "run-plain");
var createB = pfRunBlock(pfB, null);
ok(createB.indexOf("격리 점검 요청") !== -1, "create-stays");
ok(createB.indexOf("모델 호출 없이 거부") !== -1,
  "create-honest");

var note = dfixAuthorityNote(pfB);
ok(note.indexOf('role="note"') !== -1, "note-role");
ok(note.indexOf("서버가 거부합니다") !== -1, "note-refuse");
ok(note.indexOf("config-unestablished") !== -1, "note-code");
ok(note.indexOf("원본·기록은 바뀌지 않습니다") !== -1,
  "note-unchanged");
var pfOk = {state: "verified",
  authority_now: {authorized: true, pfid: "pf-1",
    verified_at: "t"}};
eq(dfixAuthorityNote(pfOk), "", "note-hidden-ok");
eq(dfixAuthorityNote({state: "verified"}), "",
  "note-hidden-missing");
console.log("PASS-COUNT " + pass);
'''


class DashboardUiCase(unittest.TestCase):
    def test_helpers_wired_and_honest(self):
        src = _script()
        for name in ("pfBlockedShort", "pfSummaryTitle",
                     "pfLeadHtml", "pfRunBlock",
                     "dfixAuthorityNote"):
            with self.subTest(name=name):
                # Declaration plus at least one call site.
                self.assertGreaterEqual(
                    src.count(name + "("), 2,
                    "helper must be called, not dead: " + name)
        prog = (L275_UI_STUBS + _decl_line(src, "esc")
                + _decl(src, "pfBlockedShort")
                + _decl(src, "pfSummaryTitle")
                + _decl(src, "pfLeadHtml")
                + _decl(src, "pfRunBlock")
                + _decl(src, "dfixAuthorityNote")
                + L275_UI_CHECKS)
        proc = _run_node(prog)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout + proc.stderr)
        self.assertIn("PASS-COUNT", proc.stdout)

    def test_start_button_has_no_client_gate(self):
        src = _script()
        m = re.search(
            r'<button class="btn sm" id="prep-dfix-go"[^>]*>',
            src)
        self.assertTrue(m, "start button markup moved?")
        tag = m.group(0)
        self.assertNotIn("disabled", tag)
        self.assertNotIn("aria-disabled", tag)
