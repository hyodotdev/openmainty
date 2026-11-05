"""Discussions UI wiring: real serve routes + cached reads.

Serve functions are the actual route handlers (payload in, dict out);
the dashboard render contract is tested against real server-shaped
payloads in node (see the second half of this file). Offline: gh and
the model are mocked at the product boundary. Py3.9-safe.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import chat as chat_mod
from openmainty import discussions as d
from openmainty import serve as serve_mod


def make_home(test):
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    prev = os.environ.get("OPENMAINTY_HOME")
    os.environ["OPENMAINTY_HOME"] = str(Path(tmp.name) / "home")
    test.addCleanup(lambda: os.environ.pop("OPENMAINTY_HOME", None)
                    if prev is None else os.environ.__setitem__(
                        "OPENMAINTY_HOME", prev))
    # Fail-fast guards: no test in this module may reach the real
    # network or a real model. Tests override exactly one boundary
    # with canned data; anything else fails loudly.
    from openmainty import muse as muse_mod

    def _no_network(*args, **kwargs):
        raise AssertionError("unexpected gh/network call in offline test")

    def _no_model(*args, **kwargs):
        raise AssertionError("unexpected model call in offline test")

    net = mock.patch.object(d._util, "run_cmd", side_effect=_no_network)
    net.start()
    test.addCleanup(net.stop)
    mod = mock.patch.object(muse_mod, "exec_prompt", side_effect=_no_model)
    mod.start()
    test.addCleanup(mod.stop)
    # The subscription gate reads ambient login/credential state, so a
    # review test would 403 under a bare env and pass under a login
    # env for no reason of its own. These tests verify validation and
    # lease codes, not the gate (the gate has its own explicit tests
    # with canned subscription_error), so the gate stays open here.
    gate = mock.patch.object(chat_mod, "subscription_error",
                             return_value=None)
    gate.start()
    test.addCleanup(gate.stop)
    return Path(tmp.name)


def norm_node(number=858, **kw):
    node = {
        "schema": 1, "repo": "o/r", "kind": "discussion",
        "id": "D858", "number": number,
        "title": "T <script>alert(1)</script>", "body": "B & body",
        "url": "https://github.com/o/r/discussions/858",
        "created_at": "2026-10-24T00:00:00+00:00",
        "updated_at": "2026-10-24T00:00:00+00:00",
        "closed": False, "closed_at": None, "upvotes": 1,
        "category": "Ideas", "author": "a",
        "upstream_labels": ["Announcement"], "labels_complete": True,
        "proposed_labels": [], "comment_count": 0, "comments": [],
        "comments_complete": True, "answer": None,
        "answer_chosenAt": None, "fetched_at": "2026-10-25T00:00:00+00:00",
    }
    node.update(kw)
    return node


RAW_NODE = {
    "id": "D858", "number": 858,
    "title": "Opus support for Android", "body": "body text",
    "url": "https://github.com/o/r/discussions/858",
    "createdAt": "2026-10-24T00:00:00+00:00",
    "updatedAt": "2026-10-24T00:00:00+00:00",
    "closed": False, "closedAt": None, "upvoteCount": 1,
    "category": {"name": "Ideas"}, "author": {"login": "a"},
    "answerChosenAt": None, "answer": None,
    "labels": {"totalCount": 0, "pageInfo": {"hasNextPage": False},
               "nodes": []},
    "comments": {"totalCount": 0,
                 "pageInfo": {"hasNextPage": False, "endCursor": None},
                 "nodes": []},
}


def fake_profile(test, checkout=None):
    from openmainty import pcconfig, repos
    repos.save({"name": "r", "github": "o/r", "baseBranch": "main",
                "knowledge": [], "checks": [], "e2e": [],
                "release": {}, "announce": {}})
    if checkout is not None:
        pc = pcconfig.load()
        pc["checkouts"]["r"] = checkout
        pcconfig.save(pc)


def list_payload(items, state="ok", disabled=False, error=None):
    return {"repo": "r", "slug": "o/r",
            "items": [{"id": "D858", "type": "discussion", "number": 858,
                       "title": items, "category": "Ideas", "comments": 0,
                       "updated_at": "2026-10-24T00:00:00+00:00",
                       "review": {"status": "unreviewed", "stale": False,
                                  "partial": False, "detail_complete": True,
                                  "labels": [], "label_facets": {},
                                  "source_unavailable": False,
                                  "job_status": "idle",
                                  "job_error": None}}],
            "status": {"state": state, "checked_at": None, "error": error,
                       "disabled": disabled, "partial": False,
                       "total_hint": 1, "backfill_after": None,
                       "note": None, "pages": 1},
            "labels": {"labels": [], "labels_complete": True, "pages": 1},
            "catalog": d.label_catalog(),
            "model": {"ok": True, "reason": None}}


class ServeListTests(unittest.TestCase):
    def setUp(self):
        make_home(self)
        fake_profile(self)

    def test_list_enriches_rows_through_real_loader(self):
        d._write_snapshot("o/r", [{
            "id": "D858", "type": "discussion", "number": 858,
            "title": "Opus <b>x</b>", "category": "Ideas", "comments": 0,
            "updated_at": "2026-10-24T00:00:00+00:00"}], "stamp")
        d.write_json(d.node_path("o/r", 858), norm_node())
        out = serve_mod.api_discussions({"repo": "r"})
        self.assertEqual(len(out["items"]), 1)
        row = out["items"][0]
        self.assertEqual(row["title"], "Opus <b>x</b>")
        self.assertEqual(row["review"]["status"], "unreviewed")
        self.assertIn("model", out)

    def test_corrupt_snapshot_repeated_poll_stays_error(self):
        path = d.snapshot_path("o/r")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{corrupt", encoding="utf-8")
        for _ in range(2):
            out = serve_mod.api_discussions({"repo": "r"})
            self.assertEqual(out["status"]["state"], "error")
            self.assertEqual(out["items"], [])
        self.assertEqual(path.read_text(encoding="utf-8"), "{corrupt")

    def test_stored_sources_without_checkout_is_unknown(self):
        d._write_snapshot("o/r", [{
            "id": "D858", "type": "discussion", "number": 858,
            "title": "t", "category": "Ideas", "comments": 0,
            "updated_at": "2026-10-24T00:00:00+00:00"}], "stamp")
        d.write_json(d.node_path("o/r", 858), norm_node())
        node = norm_node()
        fp = d.semantic_fingerprint(node)
        state = d._blank_review("o/r", 858)
        state.update({"status": "reviewed", "fingerprint": fp,
                      "report": {"summary": "s", "findings": ["f"]},
                      "labels": [], "label_facets": {},
                      "source_evidence": [{"path": "a.py",
                                           "sha256": "x"}]})
        d._write_review("o/r", 858, state)
        out = serve_mod.api_discussions({"repo": "r"})
        self.assertEqual(out["items"][0]["review"]["status"], "unknown")

    def test_list_detail_agree_on_linked_changed_source(self):
        co = Path(os.environ["OPENMAINTY_HOME"]) / "co"
        co.mkdir()
        (co / "a.py").write_text("v2\n")
        fake_profile(self, checkout=str(co))
        d._write_snapshot("o/r", [{
            "id": "D858", "type": "discussion", "number": 858,
            "title": "t", "category": "Ideas", "comments": 0,
            "updated_at": "2026-10-24T00:00:00+00:00"}], "stamp")
        d.write_json(d.node_path("o/r", 858), norm_node())
        node = norm_node()
        fp = d.semantic_fingerprint(node)
        state = d._blank_review("o/r", 858)
        state.update({"status": "reviewed", "fingerprint": fp,
                      "source_evidence": [{"path": "a.py",
                                           "sha256": "old"}]})
        d._write_review("o/r", 858, state)
        listed = serve_mod.api_discussions({"repo": "r"})
        detail = serve_mod.api_discussions_node({"repo": "r",
                                                 "number": "858"})
        self.assertEqual(
            listed["items"][0]["review"]["status"], "stale")
        self.assertEqual(detail["review"]["status"], "stale")

    def test_cached_reads_ignore_model_gate(self):
        d._write_snapshot("o/r", [], "stamp")
        with mock.patch.object(chat_mod, "subscription_error",
                               return_value="no login"):
            listed = serve_mod.api_discussions({"repo": "r"})
            self.assertEqual(listed["items"], [])
            with self.assertRaises(serve_mod.ApiError) as ctx:
                serve_mod.api_discussions_review(
                    {"repo": "r", "number": "858", "by": "owner",
                     "confirm": True})
            self.assertEqual(ctx.exception.status, 403)
        with mock.patch.object(d, "start_review") as started:
            with mock.patch.object(chat_mod, "subscription_error",
                                   return_value="no login"):
                with self.assertRaises(serve_mod.ApiError):
                    serve_mod.api_discussions_review(
                        {"repo": "r", "number": "858", "by": "owner",
                         "confirm": True})
            started.assert_not_called()

class ServeDetailTests(unittest.TestCase):
    def setUp(self):
        make_home(self)
        fake_profile(self)

    def test_node_404_until_detail_fetched(self):
        with self.assertRaises(serve_mod.ApiError) as ctx:
            serve_mod.api_discussions_node({"repo": "r",
                                            "number": "858"})
        self.assertEqual(ctx.exception.status, 404)

    def test_detail_post_then_cached_get(self):
        with mock.patch.object(d, "_gh_graphql",
                               return_value={"repository": {
                                   "discussion": RAW_NODE}}):
            posted = serve_mod.api_discussions_detail(
                {"repo": "r", "number": "858", "confirm": True})
        self.assertEqual(posted["node"]["number"], 858)
        got = serve_mod.api_discussions_node({"repo": "r",
                                              "number": "858"})
        self.assertEqual(got["node"]["title"],
                         "Opus support for Android")

    def test_detail_busy_is_409(self):
        # Same-process flock would re-acquire: simulate busy at the
        # acquire boundary.
        from openmainty import sched as sched_mod
        with mock.patch.object(sched_mod, "acquire_worker_lease",
                               return_value=None):
            with self.assertRaises(serve_mod.ApiError) as ctx:
                serve_mod.api_discussions_detail(
                    {"repo": "r", "number": "858", "confirm": True})
        self.assertEqual(ctx.exception.status, 409)

    def test_detail_needs_confirm(self):
        with self.assertRaises(serve_mod.ApiError) as ctx:
            serve_mod.api_discussions_detail({"repo": "r",
                                              "number": "858"})
        self.assertEqual(ctx.exception.status, 400)

    def test_review_validation_errors(self):
        co = Path(os.environ["OPENMAINTY_HOME"]) / "co"
        co.mkdir()
        (co / "a.py").write_text("print(1)\n", encoding="utf-8")
        fake_profile(self, checkout=str(co))
        from openmainty import sched as sched_mod
        cases = [
            ({"repo": "", "number": "858", "by": "owner",
              "confirm": True}, 400),
            ({"repo": "r", "number": "858", "by": "worker",
              "confirm": True}, 400),
            ({"repo": "r", "number": "858", "by": "owner"}, 400),
            ({"repo": "r", "number": "D_1", "by": "owner",
              "confirm": True}, 400),
            # Traversal and missing files are 400 even with checkout.
            ({"repo": "r", "number": "858", "by": "owner",
              "confirm": True, "evidence": ["../../x"]}, 400),
            ({"repo": "r", "number": "858", "by": "owner",
              "confirm": True, "evidence": ["nope.py"]}, 400),
        ]
        for payload, status in cases:
            with self.assertRaises(serve_mod.ApiError,
                                   msg=str(payload)) as ctx:
                serve_mod.api_discussions_review(payload)
            self.assertEqual(ctx.exception.status, status, payload)
        # A valid evidence path passes validation and reaches the
        # lease: busy reads 409, and a free worker starts the review.
        busy = {"repo": "r", "number": "858", "by": "owner",
                "confirm": True, "evidence": ["a.py"]}
        # The worker lease is genuinely per-process reentrant, so busy
        # is simulated at the acquire boundary — but start_review
        # refreshes the discussion BEFORE acquiring, so the fetch
        # boundary is canned too (no network in offline tests).
        with mock.patch.object(d, "fetch_node",
                               return_value=norm_node()), \
                mock.patch.object(sched_mod, "acquire_worker_lease",
                                  return_value=None):
            with self.assertRaises(serve_mod.ApiError) as ctx:
                serve_mod.api_discussions_review(dict(busy))
        self.assertEqual(ctx.exception.status, 409)
        with mock.patch.object(d, "start_review",
                               return_value={"started": True}) as started:
            out = serve_mod.api_discussions_review(dict(busy))
        self.assertTrue(out["started"])
        _, kwargs = started.call_args
        self.assertEqual(kwargs.get("evidence_paths"), ["a.py"])

    def test_review_busy_is_409(self):
        from openmainty import sched as sched_mod
        d.write_json(d.node_path("o/r", 858), norm_node())
        with mock.patch.object(d, "fetch_node",
                               return_value=norm_node()), \
                mock.patch.object(sched_mod, "acquire_worker_lease",
                                  return_value=None):
            with self.assertRaises(serve_mod.ApiError) as ctx:
                serve_mod.api_discussions_review(
                    {"repo": "r", "number": "858", "by": "owner",
                     "confirm": True})
        self.assertEqual(ctx.exception.status, 409)

    def test_evidence_needs_linked_checkout(self):
        with self.assertRaises(serve_mod.ApiError) as ctx:
            serve_mod.api_discussions_review(
                {"repo": "r", "number": "858", "by": "owner",
                 "confirm": True, "evidence": ["a.py"]})
        self.assertEqual(ctx.exception.status, 409)

    def test_twenty_six_files_rejected_with_no_worker(self):
        co = Path(os.environ["OPENMAINTY_HOME"]) / "co26"
        co.mkdir(parents=True, exist_ok=True)
        names = []
        for i in range(26):
            name = f"f{i:02d}.py"
            (co / name).write_text("x\n", encoding="utf-8")
            names.append(name)
        fake_profile(self, checkout=str(co))
        with mock.patch.object(
                d, "start_review",
                side_effect=AssertionError(
                    "26 files must never reach the worker")):
            with self.assertRaises(serve_mod.ApiError) as ctx:
                serve_mod.api_discussions_review(
                    {"repo": "r", "number": "858", "by": "owner",
                     "confirm": True, "evidence": names})
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("too many", str(ctx.exception.message))

    def test_sources_listing_and_traversal(self):
        co = Path(os.environ["OPENMAINTY_HOME"]) / "co"
        (co / "sub").mkdir(parents=True)
        (co / "top.py").write_text("x\n")
        (co / "sub" / "deep.py").write_text("y\n")
        (co / ".hidden").write_text("z\n")
        (co / "node_modules").mkdir()
        fake_profile(self, checkout=str(co))
        top = serve_mod.api_discussions_sources({"repo": "r", "dir": ""})
        self.assertIn("sub", top["dirs"])
        self.assertTrue(any(f["name"] == "top.py"
                            for f in top["files"]))
        self.assertFalse(any(f["name"] == ".hidden"
                             for f in top["files"]))
        sub = serve_mod.api_discussions_sources({"repo": "r",
                                                 "dir": "sub"})
        self.assertTrue(any(f["name"] == "deep.py"
                            for f in sub["files"]))
        with self.assertRaises(serve_mod.ApiError) as ctx:
            serve_mod.api_discussions_sources({"repo": "r",
                                               "dir": "../.."})
        self.assertEqual(ctx.exception.status, 400)

    def test_sources_listing_rejects_hidden_and_alias_dirs(self):
        co = Path(os.environ["OPENMAINTY_HOME"]) / "co"
        (co / "sub").mkdir(parents=True, exist_ok=True)
        (co / "sub" / "deep.py").write_text("y\n")
        (co / "node_modules").mkdir(exist_ok=True)
        (co / "src").mkdir(exist_ok=True)
        (co / "src" / "a.py").write_text("x\n")
        fake_profile(self, checkout=str(co))
        # Ordinary trailing slash reads the same folder.
        plain = serve_mod.api_discussions_sources({"repo": "r",
                                                  "dir": "src"})
        slashed = serve_mod.api_discussions_sources({"repo": "r",
                                                     "dir": "src/"})
        self.assertEqual(plain, slashed)
        for bad in (".hidden", "node_modules", "pods", "PODS",
                    "sub/../node_modules", "sub//deep"):
            with self.assertRaises(serve_mod.ApiError) as ctx:
                serve_mod.api_discussions_sources({"repo": "r",
                                                   "dir": bad})
            self.assertEqual(ctx.exception.status, 400)
        # Symlink aliases must not bypass the exclusion; loops are a
        # structured 400, never an uncaught resolve exception.
        try:
            os.symlink("node_modules", co / "alias_nm")
            os.symlink("alias_loop", co / "alias_loop")
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaises(serve_mod.ApiError) as ctx:
            serve_mod.api_discussions_sources({"repo": "r",
                                               "dir": "alias_nm"})
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(serve_mod.ApiError) as ctx:
            serve_mod.api_discussions_sources({"repo": "r",
                                               "dir": "alias_loop"})
        self.assertEqual(ctx.exception.status, 400)

    def test_sources_listing_file_and_root_shapes(self):
        co = Path(os.environ["OPENMAINTY_HOME"]) / "co"
        (co / "sub").mkdir(parents=True, exist_ok=True)
        (co / "sub" / "deep.py").write_text("y\n")
        fake_profile(self, checkout=str(co))
        top = serve_mod.api_discussions_sources({"repo": "r", "dir": ""})
        self.assertIn("sub", top["dirs"])
        dot = serve_mod.api_discussions_sources({"repo": "r", "dir": "."})
        self.assertEqual(dot, top)
        sub = serve_mod.api_discussions_sources({"repo": "r",
                                                 "dir": "sub"})
        slashed = serve_mod.api_discussions_sources({"repo": "r",
                                                     "dir": "sub/"})
        self.assertEqual(sub, slashed)
        self.assertTrue(any(f["name"] == "deep.py"
                            for f in sub["files"]))
        # A regular file or missing path keeps the old 404 contract.
        for missing in ("sub/deep.py", "nope"):
            with self.assertRaises(serve_mod.ApiError) as ctx:
                serve_mod.api_discussions_sources({"repo": "r",
                                                   "dir": missing})
            self.assertEqual(ctx.exception.status, 404)
        # A dangling alias resolves to a missing target: still 404.
        try:
            os.symlink("gone-nowhere", co / "dangling")
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaises(serve_mod.ApiError) as ctx:
            serve_mod.api_discussions_sources({"repo": "r",
                                               "dir": "dangling"})
        self.assertEqual(ctx.exception.status, 404)

    def test_routes_registered(self):
        import inspect
        src = inspect.getsource(serve_mod)
        for path, fn in (("/api/discussions/refresh",
                          "api_discussions_refresh"),
                         ("/api/discussions/detail",
                          "api_discussions_detail"),
                         ("/api/discussions/attempt",
                          "api_discussions_attempt"),
                         ("/api/discussions/review",
                          "api_discussions_review")):
            self.assertIn(path, src)
            self.assertIn(fn, src)
        self.assertIn('"/api/discussions"', src)
        self.assertIn('"/api/discussions/node"', src)
        self.assertIn('"/api/discussions/sources"', src)
        for fn in ("api_discussions", "api_discussions_node",
                   "api_discussions_sources", "api_discussions_attempt",
                   "api_discussions_refresh",
                   "api_discussions_detail", "api_discussions_review"):
            self.assertTrue(callable(getattr(serve_mod, fn)), fn)

    def test_attempt_endpoint_reads_one_artifact(self):
        art = {"schema": 1, "rid": "r1", "outcome": "done",
               "input_snapshot": {"discussion": "x", "sources": []}}
        d._write_review("o/r", 858, d._blank_review("o/r", 858))
        slug = d._slug_dir("o/r")
        (slug / "review-858-r1.json").write_text(
            json.dumps(art), encoding="utf-8")
        out = serve_mod.api_discussions_attempt(
            {"repo": "r", "number": "858", "artifact": "review-858-r1.json"})
        self.assertEqual(out["attempt"]["rid"], "r1")
        with self.assertRaises(serve_mod.ApiError) as ctx:
            serve_mod.api_discussions_attempt(
                {"repo": "r", "number": "858",
                 "artifact": "../review-858-r1.json"})
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(serve_mod.ApiError) as ctx:
            serve_mod.api_discussions_attempt(
                {"repo": "r", "number": "858",
                 "artifact": "review-999-r1.json"})
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(serve_mod.ApiError) as ctx:
            serve_mod.api_discussions_attempt(
                {"repo": "r", "number": "858",
                 "artifact": "review-858-nope.json"})
        self.assertEqual(ctx.exception.status, 404)

    def test_facet_map_survives_list(self):
        d._write_snapshot("o/r", [{
            "id": "D858", "type": "discussion", "number": 858,
            "title": "t", "category": "Ideas", "comments": 0,
            "updated_at": "2026-10-24T00:00:00+00:00"}], "stamp")
        d.write_json(d.node_path("o/r", 858), norm_node())
        state = d._blank_review("o/r", 858)
        state.update({"status": "reviewed",
                      "fingerprint": d.semantic_fingerprint(norm_node()),
                      "report": {"summary": "s", "findings": ["f"]},
                      "labels": ["android"],
                      "label_facets": {"platform": ["android"]}})
        d._write_review("o/r", 858, state)
        out = serve_mod.api_discussions({"repo": "r"})
        self.assertEqual(out["items"][0]["review"]["label_facets"],
                         {"platform": ["android"]})


NODE_PRELUDE = r"""
let view = {id: "r"};
let repoTab = "community";
const CSS = {escape: (s) => String(s)};
let confirmCalls = 0;
const window = {confirm: (m) => { confirmCalls++; return true; }};
let toasts = [];
function toast(m) { toasts.push(String(m)); }
function fmtKSThm(s) { return "KST:" + String(s || ""); }
async function restoreReviewStateAsync(box, keep) {}
function takeStashedReviewState() { return null; }
function captureReviewState(box) { return null; }
function findSelects(html) {
  const out = [];
  const re = /<select[^>]*data-dkf="([^"]+)"[^>]*>([\s\S]*?)<\/select>/g;
  let m;
  while ((m = re.exec(html))) {
    const opts = [...m[2].matchAll(/<option value="([^"]*)"( selected)?>/g)];
    const sel = opts.find(o => o[2]) || opts[0];
    const el = {dataset: {dkf: m[1]}, value: sel ? sel[1] : "", _h: null,
      // Faithful to the loader's wiring: paintRepoCommunity assigns
      // s.onchange = handler, and the test fires that same property.
      set onchange(f) { this._h = f; },
      get onchange() { return this._h; }};
    out.push(el);
  }
  return out;
}
function mkBox() {
  return {_html: "", dataset: {}, _cache: {},
    set innerHTML(v) { this._html = String(v); this._cache = {}; },
    get innerHTML() { return this._html; },
    querySelectorAll(sel) {
      // Stable element identity per rendered HTML, like a real DOM:
      // repeated queries return the same objects, so handlers wired
      // by the loader land on the elements the test drives.
      if (sel === "[data-dkf]") {
        const key = sel + "#" + this._html;
        if (!this._cache[key]) this._cache[key] = findSelects(this._html);
        return this._cache[key];
      }
      return [];
    },
    querySelector(sel) { return null; }};
}
const tabEl = mkBox();
function $(id) { return tabEl; }
let LIST = null, NODE = null, SOURCES = null, ATTEMPT = null;
let nodeCalls = 0, postBodies = {};
let nodeSeq = [];
function jget(url) {
  return new Promise((res, rej) => setTimeout(() => {
    if (url.startsWith("/api/discussions?repo=")) return res(JSON.parse(JSON.stringify(LIST)));
    if (url.startsWith("/api/discussions/node")) {
      nodeCalls++;
      const next = nodeSeq.length ? nodeSeq.shift() : NODE;
      if (next instanceof Error) return rej(next);
      return res(JSON.parse(JSON.stringify(next)));
    }
    if (url.startsWith("/api/discussions/sources")) return res(JSON.parse(JSON.stringify(SOURCES)));
    if (url.startsWith("/api/discussions/attempt")) return res(JSON.parse(JSON.stringify(ATTEMPT)));
    return rej(new Error("unexpected GET " + url));
  }, 5));
}
function jpost(url, body) {
  postBodies[url] = body;
  return new Promise((res, rej) => setTimeout(() => {
    if (url === "/api/discussions/detail") return res({ok: true});
    if (url === "/api/discussions/refresh") return res({items: [], status: LIST.status});
    if (url === "/api/discussions/review") return res(REVIEW_RESP);
    return rej(new Error("unexpected POST " + url));
  }, 5));
}
let REVIEW_RESP = {started: true};
const document = {querySelector(sel) { return null; }};
"""


def community_js():
    src = Path(__file__).resolve().parent.parent / "src" / "openmainty" / "dashboard.html"
    html = src.read_text(encoding="utf-8")
    esc_line = next(line for line in html.splitlines()
                    if line.startswith("function esc("))
    start = html.index("/* \ucee4\ubba4\ub2c8\ud2f0(Discussions)")
    end = html.index("/* \ubb36\uc74c \uc0c1\uc138", start)
    return esc_line + "\n" + html[start:end]


def run_driver(driver_js, prelude_extra=""):
    import subprocess
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(NODE_PRELUDE + prelude_extra + community_js()
                 + "\n" + driver_js)
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


def server_shaped_list():
    return {
        "repo": "r", "slug": "o/r",
        "items": [
            {"id": "D1", "type": "discussion", "number": 1,
             "title": " Opus <script>alert(1)</script>",
             "category": "Ideas", "comments": 2,
             "updated_at": "2026-10-24T00:00:00+00:00",
             "url": "https://github.com/o/r/discussions/1",
             "review": {"status": "reviewed", "stale": False,
                        "partial": False, "detail_complete": True,
                        "labels": ["android", "feature"],
                        "label_facets": {"platform": ["android"],
                                         "type": ["feature"]},
                        "source_unavailable": False,
                        "job_status": "idle", "job_error": None}},
            {"id": "D2", "type": "discussion", "number": 858,
             "title": "Plain question",
             "category": "Q&A", "comments": 0,
             "updated_at": "2026-10-24T00:00:00+00:00",
             "url": "https://github.com/o/r/discussions/858",
             "review": {"status": "unreviewed", "stale": False,
                        "partial": False, "detail_complete": True,
                        "labels": [], "label_facets": {},
                        "source_unavailable": False,
                        "job_status": "idle", "job_error": None}},
        ],
        "status": {"state": "ok",
                   "checked_at": "2026-10-25T00:00:00+00:00",
                   "error": None, "disabled": False, "partial": False,
                   "total_hint": 2, "backfill_after": None,
                   "note": None, "pages": 1,
                   "watermark": "2026-10-24T00:00:00+00:00",
                   "cursor": "C1"},
        "labels": {"labels": [{"name": "bug"}], "labels_complete": True,
                   "pages": 1},
        "catalog": {"status": {"unreviewed": "U", "reviewed": "R",
                               "stale-review": "S", "needs-action": "A",
                               "needs-review": "N"},
                    "platform": {"android": "A", "ios": "I", "web": "W",
                                 "unknown": "U"},
                    "type": {"feature": "F", "question": "Q", "bug": "B",
                             "discussion": "D", "docs": "C",
                             "unknown": "U"}},
        "model": {"ok": True, "reason": None},
    }


def server_shaped_node():
    return {
        "repo": "r", "slug": "o/r",
        "node": {
            "id": "D1", "number": 1,
            "title": "Opus <script>alert(1)</script>",
            "body": "Line1 & <b>bold</b>",
            "url": "https://github.com/o/r/discussions/1",
            "created_at": "2026-10-24T00:00:00+00:00",
            "updated_at": "2026-10-24T00:00:00+00:00",
            "closed": False, "category": "Ideas", "author": "a",
            "upstream_labels": ["Announcement"],
            "labels_complete": True, "comment_count": 1,
            "comments": [{
                "id": "C1", "author": "b",
                "body": "evil <img src=x onerror=y>",
                "created_at": "2026-10-24T01:00:00+00:00",
                "updated_at": "2026-10-24T01:00:00+00:00",
                "url": "https://github.com/o/r/discussions/1#c1",
                "is_answer": False, "replies": [],
                "reply_count": 0, "replies_complete": True}],
            "comments_complete": True, "answer": None,
        },
        "review": {
            "status": "reviewed", "stale": False, "partial": False,
            "partial_reasons": [], "detail_complete": True,
            "source_unavailable": False, "source_changed": False,
            "source_checked": True, "fingerprint": "fp",
            "current_fingerprint": "fp", "labels": ["android"],
            "label_facets": {"platform": ["android"]},
            "report": {"summary": "SUM <b>x</b>",
                       "findings": ["F1"]},
            "artifact": "review-1-r1.json",
            "evidence": [{"url": "https://github.com/o/r/discussions/1",
                          "note": "title"}],
            "source_evidence": [{"path": "a.py",
                                 "sha256": "abc123def456",
                                 "bytes_sent": 20, "bytes_total": 20,
                                 "truncated": False}],
            "source_cited": ["a.py"],
            "source_scope": {"requested": ["a.py", "big.py"],
                             "sent": ["a.py"],
                             "excluded": [{"path": "big.py",
                                           "sha256": "zzz",
                                           "bytes_total": 50000,
                                           "bytes_sent": 0,
                                           "reason": "source budget "
                                                     "exhausted: file not "
                                                     "sent"}],
                             "cited": ["a.py"]},
            "source_current": [{"path": "a.py",
                                "sha256": "abc123def456",
                                "reason": None}],
            "reply_draft": "DRAFT <i>y</i>",
            "job": {"status": "done", "rid": "r1",
                     "artifact": "review-1-r1.json"},
            "history": [{"at": "2026-10-25T01:00:00+00:00",
                         "event": "job-done",
                         "artifact": "review-1-r1.json"}],
        },
        "model": {"ok": True, "reason": None},
    }


def server_shaped_attempt():
    return {
        "repo": "r", "slug": "o/r", "number": 1,
        "artifact": "review-1-r1.json",
        "attempt": {
            "schema": 1, "repo": "o/r",
            "kind": "discussion-review-attempt",
            "number": 1, "rid": "r1", "outcome": "done",
            "fingerprint": "fp",
            "at": "2026-10-25T02:00:00+00:00",
            "prompt": "PROMPT body with source chunks",
            "input_snapshot": {
                "discussion": "Opus <b>body</b>",
                "discussion_bytes_total": 100,
                "discussion_bytes_sent": 100,
                "sources": [{"path": "a.py",
                             "sha256": "abc123def456",
                             "bytes_total": 20, "bytes_sent": 20,
                             "truncated": False,
                             "excerpt": "print(<x>)\n"}],
                "sources_requested": ["a.py", "big.py"],
                "sources_excluded": [{"path": "big.py",
                                      "sha256": "zzz",
                                      "bytes_total": 50000,
                                      "bytes_sent": 0,
                                      "reason": "source budget exhausted: "
                                                "file not sent"}],
            },
            "partial": ["source budget exhausted: later files cut"],
            "raw_output": "terminal line",
            "raw_output_meta": {"total": 50, "sent": 50,
                                "capped": False},
            "report": {"summary": "SUM", "findings": ["F1"],
                       "source_files": ["a.py"]},
            "raw_report_keys": ["summary", "findings"],
            "error": None, "usage": {"exit_code": 0},
            "source_evidence": [{"path": "a.py",
                                 "sha256": "abc123def456"}],
        },
    }


class NodeRenderTests(unittest.TestCase):
    def test_list_renders_escaped_rows_without_orphan_numbers(self):
        out = run_driver("""
LIST = %s;
(async () => {
  await paintRepoCommunity({name: "r"});
  const html = tabEl.innerHTML;
  const rows = (html.match(/data-discrow="/g) || []).length;
  console.log(JSON.stringify({
    rows,
    hasTitle: html.includes("Opus "),
    noRawScript: !html.includes("<script>"),
    escScript: html.includes("&lt;script&gt;"),
    ariaFalse: (html.match(/aria-expanded="false"/g) || []).length,
    textareas: (html.match(/<textarea/g) || []).length,
    unknownChips: (html.match(/미분류/g) || []).length,
    noRawUnknown: !html.includes(">unknown ·"),
    width390: !/width:\\s*\\d{3,}px/.test(html),
    wrap: html.includes("overflow-wrap:anywhere") || html.includes("evlink"),
  }));
})();
""" % json.dumps(server_shaped_list()))
        got = out[-1]
        self.assertEqual(got["rows"], 2)
        self.assertTrue(got["hasTitle"])
        self.assertTrue(got["noRawScript"])
        self.assertTrue(got["escScript"])
        self.assertEqual(got["ariaFalse"], 2)
        self.assertEqual(got["textareas"], 0)
        # One quiet 미분류 for the unreviewed row, never 3 invented chips.
        self.assertEqual(got["unknownChips"], 1)
        self.assertTrue(got["noRawUnknown"])
        self.assertTrue(got["width390"])

    def test_filter_onchange_shows_matching_rows(self):
        out = run_driver("""
LIST = %s;
(async () => {
  await paintRepoCommunity({name: "r"});
  const sels = tabEl.querySelectorAll("[data-dkf]");
  const plat = sels.find(s => s.dataset.dkf === "platform");
  plat.value = "android";
  await plat.onchange();
  const html = tabEl.innerHTML;
  const rows = (html.match(/data-discrow="/g) || []).length;
  console.log(JSON.stringify({rows, keptOpus: html.includes("data-discrow=\\"1\\""),
    droppedPlain: !html.includes("data-discrow=\\"858\\"")}));
})();
""" % json.dumps(server_shaped_list()))
        got = out[-1]
        self.assertEqual(got["rows"], 1)
        self.assertTrue(got["keptOpus"])
        self.assertTrue(got["droppedPlain"])

    def test_toggle_opens_inline_detail_with_focus_target(self):
        out = run_driver("""
LIST = %s;
NODE = %s;
(async () => {
  await paintRepoCommunity({name: "r"});
  await discToggleDetail({name: "r"}, "r:1", LIST.catalog);
  const html = tabEl.innerHTML;
  console.log(JSON.stringify({
    expanded: (html.match(/aria-expanded="true"/g) || []).length,
    controls: html.includes('aria-controls="disc-inspector"'),
    card: html.includes('id="disc-card-1"'),
    focus: html.includes('data-discfocus="1"'),
    draftAreas: (html.match(/<textarea/g) || []).length,
    noRawScript: !html.includes("<script>"),
    summary: html.includes("SUM "),
    srcEv: html.includes("a.py"),
    cited: html.includes("인용됨"),
    sameAsReviewed: html.includes("검토 시점과 동일"),
    scopeLine: html.includes("모델에 전달 1건"),
    excluded: html.includes("big.py") && html.includes("보내지 않음"),
    viewer: html.includes("검토에 쓴 원문·코드 근거 보기"),
    noRawKeep: !html.includes("보관:"),
    attempt: html.includes("review-1-r1.json"),
    posts: Object.keys(postBodies),
  }));
})();
""" % (json.dumps(server_shaped_list()),
       json.dumps(server_shaped_node())))
        got = out[-1]
        self.assertEqual(got["expanded"], 1)
        self.assertTrue(got["controls"])
        self.assertTrue(got["card"])
        self.assertTrue(got["focus"])
        self.assertEqual(got["draftAreas"], 1)
        self.assertTrue(got["noRawScript"])
        self.assertTrue(got["summary"])
        self.assertTrue(got["srcEv"])
        self.assertTrue(got["cited"])
        self.assertTrue(got["sameAsReviewed"])
        self.assertTrue(got["scopeLine"])
        self.assertTrue(got["excluded"])
        self.assertTrue(got["viewer"])
        self.assertTrue(got["noRawKeep"])
        self.assertTrue(got["attempt"])
        # Toggle opens read-only: cached GET, never the fetch POST.
        # Product design source, dashboard.html discToggleDetail:
        # "열기는 저장된 기록부터 읽는다: 네트워크 새로 가져오기(POST)를
        #  먼저 쏘지 않는다. 검토 시작도 하지 않는다." POST happens only
        # on the explicit "원문 새로 가져오기" click (discFetchNode:
        # "사용자 클릭 때만 기존 POST /api/discussions/detail을 호출한다").
        self.assertNotIn("/api/discussions/detail", got["posts"])

    def test_attempt_viewer_renders_exact_model_input(self):
        out = run_driver("""
ATTEMPT = %s;
console.log(JSON.stringify({
  html: discAttemptHtml(ATTEMPT.attempt),
}));
""" % json.dumps(server_shaped_attempt()))
        html = out[-1]["html"]
        # Exact sent discussion text and sent code excerpt are visible.
        self.assertIn("Opus &lt;b&gt;body&lt;/b&gt;", html)
        self.assertIn("print(&lt;x&gt;)", html)
        # Scope honesty: requested/sent/excluded/cited counts.
        self.assertIn("요청 2건", html)
        self.assertIn("전달 1건", html)
        self.assertIn("제외 1건", html)
        # Receipt plus technical record, not a bare filename.
        self.assertIn("r1", html)
        self.assertIn("보고서 키:", html)
        self.assertIn("기술 기록 보기", html)

    def test_attempt_load_wiring_fetches_and_paints(self):
        out = run_driver("""
LIST = %s;
NODE = %s;
ATTEMPT = %s;
const boxes = {};
document.querySelector = (sel) => {
  const m = /^\\[data-discattemptbox="(.+)"\\]$/.exec(sel);
  if (!m) return null;
  if (!boxes[m[1]]) boxes[m[1]] = {innerHTML: ""};
  return boxes[m[1]];
};
(async () => {
  await discLoadAttempt({name: "r"}, "r:1|review-1-r1.json");
  console.log(JSON.stringify({box: boxes["r:1"].innerHTML}));
})();
""" % (json.dumps(server_shaped_list()),
       json.dumps(server_shaped_node()),
       json.dumps(server_shaped_attempt())))
        html = out[-1]["box"]
        self.assertIn("print(&lt;x&gt;)", html)
        self.assertIn("Opus &lt;b&gt;body&lt;/b&gt;", html)

    def test_paused_repo_shows_no_fabricated_next_check(self):
        out = run_driver("""
LIST = %s;
(async () => {
  await paintRepoCommunity({name: "r", sched_enabled: false,
    next_scan: "2026-10-25T09:26:00+00:00"});
  const paused = tabEl.innerHTML;
  await paintRepoCommunity({name: "r", sched_enabled: true,
    next_scan: "2026-10-25T09:26:00+00:00"});
  const live = tabEl.innerHTML;
  delete discCache["r"];
  await paintRepoCommunity({name: "r", sched_enabled: true});
  const unknown = tabEl.innerHTML;
  console.log(JSON.stringify({paused, live, unknown}));
})();
""" % json.dumps(server_shaped_list()))
        self.assertIn("자동 확인 일시정지됨", out[-1]["paused"])
        self.assertNotIn("다음 확인 ~", out[-1]["paused"])
        # Live shows the actual planned scan, not checked_at + 30m.
        self.assertIn("다음 확인 ~", out[-1]["live"])
        self.assertIn("09:26", out[-1]["live"])
        self.assertIn("예정된 저장소 검사", out[-1]["live"])
        self.assertNotIn("00:30", out[-1]["live"])
        # No planned scan: honest unknown, never a fabricated time.
        self.assertIn("다음 일정 미정", out[-1]["unknown"])
        self.assertNotIn("다음 확인 ~", out[-1]["unknown"])

    def test_manual_check_does_not_move_planned_scan(self):
        lst = list_payload("t")
        lst["status"] = dict(lst["status"],
                             checked_at="2026-10-25T09:05:00+00:00")
        out = run_driver("""
LIST = %s;
(async () => {
  await paintRepoCommunity({name: "r", sched_enabled: true,
    next_scan: "2026-10-25T09:26:00+00:00"});
  console.log(JSON.stringify({html: tabEl.innerHTML}));
})();
""" % json.dumps(lst))
        html = out[-1]["html"]
        self.assertIn("09:05", html)
        self.assertIn("09:26", html)
        self.assertNotIn("09:35", html)

    def test_changed_schedule_repaints_header(self):
        out = run_driver("""
LIST = %s;
(async () => {
  await paintRepoCommunity({name: "r", sched_enabled: true,
    next_scan: "2026-10-25T09:26:00+00:00"});
  const first = tabEl.innerHTML;
  delete discCache["r"];
  await paintRepoCommunity({name: "r", sched_enabled: true,
    next_scan: "2026-10-25T10:26:00+00:00"});
  const second = tabEl.innerHTML;
  console.log(JSON.stringify({first, second}));
})();
""" % json.dumps(server_shaped_list()))
        self.assertIn("09:26", out[-1]["first"])
        self.assertIn("10:26", out[-1]["second"])
        self.assertNotIn("10:26", out[-1]["first"])

    def test_job_korean_copy_and_audit_in_secondary(self):
        out = run_driver("""
LIST = %s;
NODE = %s;
(async () => {
  await paintRepoCommunity({name: "r"});
  await discToggleDetail({name: "r"}, "r:1", LIST.catalog);
  console.log(JSON.stringify({html: tabEl.innerHTML}));
})();
""" % (json.dumps(server_shaped_list()),
       json.dumps(server_shaped_node())))
        html = out[-1]["html"]
        self.assertIn("작업: 완료", html)
        self.assertNotIn("작업: done", html)
        self.assertIn("검토 요약", html)
        # The audit actor lives in the secondary record only.
        self.assertNotIn("요청자: owner", html)
        self.assertIn("담당:", html)

    def test_failed_state_and_next_action_visible_outside_record(self):
        node = server_shaped_node()
        err = dict(node["review"],
                   job={"status": "error", "rid": "r1",
                         "error": "모델 할당량 소진(quota/rate limit, "
                                  "exit 1); 한도 회복 후 수동으로 다시 "
                                  "요청 — 다시 로그인할 필요 없음. "
                                  "시도 기록은 보관됨 — 수동으로 다시 요청",
                         "error_kind": "quota"})
        out = run_driver("""
LIST = %s;
NODE = %s;
(async () => {
  await paintRepoCommunity({name: "r"});
  await discToggleDetail({name: "r"}, "r:1", LIST.catalog);
  console.log(JSON.stringify({html: tabEl.innerHTML}));
})();
""" % (json.dumps(server_shaped_list()),
       json.dumps(dict(node, review=err))))
        html = out[-1]["html"]
        # The failure and its next action read in the primary flow,
        # not only inside the collapsed technical record.
        self.assertIn("검토 실패", html)
        self.assertIn("한도 회복", html)
        self.assertLess(html.index("검토 실패"),
                        html.index("실행 기록 보기"))
        self.assertIn("작업: 실패", html)

    def test_attempt_heading_human_not_raw_rid(self):
        out = run_driver("""
ATTEMPT = %s;
console.log(JSON.stringify({
  html: discAttemptHtml(ATTEMPT.attempt),
}));
""" % json.dumps(server_shaped_attempt()))
        html = out[-1]["html"]
        self.assertIn("검토에 사용한 입력", html)
        self.assertIn("#1", html)
        self.assertNotIn("시도 원문·근거 fp12", html)

    def test_changed_source_marked_not_blanket_verified(self):
        node = server_shaped_node()
        changed = dict(node["review"],
                       source_current=[{"path": "a.py",
                                        "sha256": "DIFFERENT",
                                        "reason": None}])
        out = run_driver("""
LIST = %s;
NODE = %s;
(async () => {
  await paintRepoCommunity({name: "r"});
  await discToggleDetail({name: "r"}, "r:1", LIST.catalog);
  console.log(JSON.stringify({html: tabEl.innerHTML}));
})();
""" % (json.dumps(server_shaped_list()),
       json.dumps(dict(node, review=changed))))
        html = out[-1]["html"]
        self.assertIn("검토 후 변경됨", html)
        self.assertNotIn("해시 확인됨", html)

    def test_detail_report_hierarchy_tiles_cards_record(self):
        out = run_driver("""
LIST = %s;
NODE = %s;
(async () => {
  await paintRepoCommunity({name: "r"});
  await discToggleDetail({name: "r"}, "r:1", LIST.catalog);
  console.log(JSON.stringify({html: tabEl.innerHTML}));
})();
""" % (json.dumps(server_shaped_list()),
       json.dumps(server_shaped_node())))
        html = out[-1]["html"]
        # Compact header: lifecycle chip + grouped classification once.
        self.assertIn("rep-head", html)
        self.assertIn("분류 · 원본", html)
        # Primary body: summary + findings with hierarchy.
        self.assertIn("검토 요약", html)
        self.assertIn("확인된 내용", html)
        # Distinct scope section: tiles + one card per file.
        self.assertIn("검토 범위와 코드 근거", html)
        self.assertIn("scope-tile", html)
        self.assertIn("srccard", html)
        # Exclusion: Korean primary, raw diagnostic secondary.
        self.assertIn("예산 초과로 보내지 않음", html)
        self.assertIn("source budget exhausted", html)
        # One collapsed execution record, not a flat dump.
        self.assertIn("실행 기록 보기", html)
        self.assertIn("작업: 완료", html)

    def test_draft_textarea_labeled_and_styled(self):
        out = run_driver("""
LIST = %s;
NODE = %s;
(async () => {
  await paintRepoCommunity({name: "r"});
  await discToggleDetail({name: "r"}, "r:1", LIST.catalog);
  console.log(JSON.stringify({html: tabEl.innerHTML}));
})();
""" % (json.dumps(server_shaped_list()),
       json.dumps(server_shaped_node())))
        html = out[-1]["html"]
        self.assertIn('<label for="disc-draft-1">회신 초안', html)
        self.assertIn('class="dscf-ro"', html)
        self.assertIn("DRAFT", html)

    def test_partial_short_primary_with_full_disclosure(self):
        node = server_shaped_node()
        reasons = ["labels paginated: first page only",
                   "source p1.ts: 20000 of 25000 bytes sent (per-file cap)",
                   "source budget exhausted: later files cut"]
        partial = dict(node["review"], partial=True,
                       partial_reasons=reasons)
        out = run_driver("""
LIST = %s;
NODE = %s;
(async () => {
  await paintRepoCommunity({name: "r"});
  await discToggleDetail({name: "r"}, "r:1", LIST.catalog);
  console.log(JSON.stringify({html: tabEl.innerHTML}));
})();
""" % (json.dumps(server_shaped_list()),
       json.dumps(dict(node, review=partial))))
        html = out[-1]["html"]
        # Short primary: first reason + remainder count, not a dump.
        self.assertIn("부분 검토: 라벨 일부만 전송됨 외 2건", html)
        self.assertNotIn("p1.ts: 25000B 중 20000B만 전송됨(파일당 상한) · "
                         "예산 초과로 일부 파일 제외됨", html)
        # Full readable reasons + raw diagnostics stay inspectable.
        self.assertIn("부분 검토 사유 보기", html)
        self.assertIn("p1.ts: 25000B 중 20000B", html)
        self.assertIn("예산 초과로 일부 파일 제외됨", html)
        self.assertIn("source p1.ts: 20000 of 25000 bytes sent", html)

    def test_attempt_viewer_parsed_discussion_and_technical_record(self):
        attempt = server_shaped_attempt()
        canon = {"title": "Opus support for Android",
                 "body": "Preview body text",
                 "category": "Ideas", "author": "someone",
                 "upvotes": 3,
                 "upstream_labels": ["android", "bug"],
                 "comments": [{"author": "c1",
                               "body": "comment body",
                               "created_at": "2026-10-25T00:00:00+00:00"}]}
        disc = json.dumps(canon, ensure_ascii=False)
        attempt["attempt"] = dict(
            attempt["attempt"],
            input_snapshot=dict(
                attempt["attempt"]["input_snapshot"],
                discussion=disc,
                discussion_bytes_total=len(disc.encode("utf-8")),
                discussion_bytes_sent=len(disc.encode("utf-8"))))
        out = run_driver("""
ATTEMPT = %s;
console.log(JSON.stringify({
  html: discAttemptHtml(ATTEMPT.attempt),
}));
""" % json.dumps(attempt))
        html = out[-1]["html"]
        # Human-readable canonical input: title/body/comments.
        # The real canonical field is upstream_labels, not labels.
        self.assertIn("Opus support for Android", html)
        self.assertIn("Preview body text", html)
        self.assertIn("comment body", html)
        self.assertIn("android, bug", html)
        # Exact sent text stays available with the byte caveat.
        self.assertIn("원문 그대로 보기", html)
        # Result in Korean; raw ids only in the technical record.
        self.assertIn("결과: 완료", html)
        self.assertIn("기술 기록 보기", html)
        self.assertIn("시도 식별자:", html)
        self.assertNotIn("결과 done", html)
        # Code caption: name + scope, no hash; hash lives in the body.
        self.assertIn("전달 20/20B", html)
        cap = html.split("<summary>")[1].split("</summary>")[0]
        self.assertNotIn("abc123", cap)
        self.assertIn("abc123def456", html)

    def test_evidence_picker_cap_refuses_with_message(self):
        out = run_driver("""
console.log(JSON.stringify({
  atCap: discEvToggle(Array.from({length: 25}, (_, i) => "f" + i + ".py"), "f26.py", true),
  free: discEvToggle(["a.py"], "b.py", true),
  again: discEvToggle(["a.py"], "a.py", true),
  uncheck: discEvToggle(["a.py", "b.py"], "a.py", false),
}));
""")
        got = out[-1]
        self.assertTrue(got["atCap"]["refused"])
        self.assertEqual(len(got["atCap"]["picked"]), 25)
        self.assertNotIn("f26.py", got["atCap"]["picked"])
        self.assertFalse(got["free"]["refused"])
        self.assertIn("b.py", got["free"]["picked"])
        self.assertFalse(got["again"]["refused"])
        self.assertEqual(got["again"]["picked"], ["a.py"])
        self.assertEqual(got["uncheck"]["picked"], ["b.py"])

    def test_evidence_picker_nav_and_selected_rows(self):
        out = run_driver("""
discEvSel["k"] = ["src/a.py", "android/Sound.kt", "x/y.py", "x/z.py",
                  "x/w.py", "x/v.py", "x/u.py"];
console.log(JSON.stringify({
  html: discEvidenceHtml("k", {dir: "android/", dirs: ["sub"],
    files: [{name: "Sound.kt", size: 37779},
            {name: "Other.kt", size: 10}], truncated: false}),
}));
""")
        html = out[-1]["html"]
        # One selection-level heading; full paths + current size only.
        self.assertIn("다음 검토에 포함할 파일 7건(아직 미실행)", html)
        self.assertIn("android/Sound.kt", html)
        self.assertIn("37779B", html)
        self.assertIn("크기 알 수 없음", html)
        self.assertEqual(html.count("미실행"), 1)
        self.assertNotIn("20000/37779", html)
        self.assertNotIn("전송", html)
        self.assertNotIn("인용", html)
        # Folder navigation: go input, breadcrumbs, name filter.
        self.assertIn('data-discevgo="k"', html)
        self.assertIn('data-discevfilter="k"', html)
        self.assertIn("현재 폴더 위치", html)
        self.assertIn('data-discevnav="k|android"', html)
        # One simple collapse pattern for more than five rows.
        self.assertIn("외 2건 모두 보기", html)
        self.assertIn("빼기", html)
        out = run_driver("""
discEvFilter["k"] = "sound";
console.log(JSON.stringify({
  html: discEvidenceHtml("k", {dir: "", dirs: [],
    files: [{name: "Sound.kt", size: 1},
            {name: "Other.kt", size: 2}], truncated: false}),
}));
""")
        html = out[-1]["html"]
        self.assertIn("Sound.kt", html)
        self.assertNotIn("Other.kt", html)
        out = run_driver("""
discEvFilter["k"] = "zzz";
console.log(JSON.stringify({
  html: discEvidenceHtml("k", {dir: "", dirs: [],
    files: [{name: "Sound.kt", size: 1}], truncated: false}),
}));
""")
        self.assertIn("찾는 이름과 맞는 파일 없음",
                      out[-1]["html"])

    def test_evidence_picker_nav_units(self):
        out = run_driver("""
discEvRememberSizes("k", {dir: "src/", dirs: [],
  files: [{name: "a.py", size: 100}]});
discEvMoreOpen["k"] = true;
discEvSel["k"] = ["src/a.py", "other/b.py", "1.py", "2.py", "3.py",
                  "4.py", "5.py"];
console.log(JSON.stringify({
  nav: discEvNavHtml("k", "a/b/"),
  sel: discEvSelHtml("k", null),
  shell: discEvidenceShellHtml("k", "a/b/", "목록 조회 실패: x", true),
}));
""")
        got = out[-1]
        # Breadcrumbs: root plus one button per nested segment.
        self.assertIn('data-discevnav="k|"', got["nav"])
        self.assertIn('data-discevnav="k|a"', got["nav"])
        self.assertIn('data-discevnav="k|a/b"', got["nav"])
        self.assertIn("현재 폴더 위치", got["nav"])
        # Typed drafts survive through data-k restore points.
        self.assertIn('data-k="discevgo:k"', got["nav"])
        self.assertIn('data-k="discevfilter:k"', got["nav"])
        # Size ledger: remembered size survives folder navigation.
        self.assertIn("src/a.py", got["sel"])
        self.assertIn("100B", got["sel"])
        self.assertIn("크기 알 수 없음", got["sel"])
        self.assertNotIn("전송", got["sel"])
        # Collapse open state restored across rerenders.
        self.assertIn("외 2건 모두 보기", got["sel"])
        self.assertIn("open", got["sel"].split("<details")[1]
                     .split(">")[0])
        # Error shell keeps draft inputs, selection rows and message.
        self.assertIn("목록 조회 실패: x", got["shell"])
        self.assertIn("다음 검토에 포함할 파일 7건(아직 미실행)",
                      got["shell"])
        self.assertIn("inline-err", got["shell"])
        # Ledger is scoped per discussion and bounded at 200 entries.
        self.assertIn("100B", got["sel"])

    def test_evidence_picker_ledger_scope_and_cap(self):
        out = run_driver("""
discEvSizeSeen["k1"] = {"a.py": 10};
discEvSel["k1"] = ["a.py"];
discEvSel["k2"] = ["a.py"];
const big = {dir: "", files: []};
for (let i = 0; i < 250; i++) big.files.push({name: "f" + i + ".py", size: i});
discEvRememberSizes("k3", big);
discEvSel["k4"] = ["keep/me.py"];
discEvRememberSizes("k4", {dir: "keep/", dirs: [],
  files: [{name: "me.py", size: 7}]});
const crowd = {dir: "crowd/", dirs: [], files: []};
for (let i = 0; i < 200; i++) crowd.files.push({name: "c" + i + ".py", size: i});
discEvRememberSizes("k4", crowd);
console.log(JSON.stringify({
  k1: discEvSelHtml("k1", null),
  k2: discEvSelHtml("k2", null),
  k4: discEvSelHtml("k4", null),
  cap: Object.keys(discEvSizeSeen["k3"]).length,
  evicted: !Object.prototype.hasOwnProperty.call(discEvSizeSeen["k3"], "f0.py"),
  newest: discEvSizeSeen["k3"]["f249.py"],
  kept: discEvSizeSeen["k4"]["keep/me.py"],
  keptLen: Object.keys(discEvSizeSeen["k4"]).length
}));
""")
        got = out[-1]
        self.assertIn("10B", got["k1"])
        self.assertIn("크기 알 수 없음", got["k2"])
        self.assertEqual(got["cap"], 200)
        self.assertTrue(got["evicted"])
        self.assertEqual(got["newest"], 249)
        # A selected file is pinned: navigating to another 200-entry
        # directory must not turn its known size back to unknown.
        self.assertEqual(got["kept"], 7)
        self.assertLessEqual(got["keptLen"], 200)
        self.assertIn("keep/me.py", got["k4"])
        self.assertIn("7B", got["k4"])

    def test_evidence_picker_handlers_end_to_end(self):
        # A faithful mini-DOM drives the ACTUAL production onclick /
        # onchange / onkeydown / ontoggle handlers. innerHTML replacement
        # detaches the focused element back to body (like a real DOM), so
        # focus/caret restoration — and the do-not-steal guard — is really
        # exercised, not stubbed to success.
        out = run_driver("""
const trail = [];
function mkEl(kind, dataset, init) {
  const el = {kind: kind, dataset: dataset,
    checked: false, value: "", open: false,
    onclick: null, onchange: null, onkeydown: null, ontoggle: null,
    focus: function() { document.activeElement = this,
      trail.push(["focus", this.kind]); },
    setSelectionRange: function(a, b) { this._sel = [a, b];
      trail.push(["sel", this.kind, a, b]); }
  };
  if (init) { Object.assign(el, init); }
  return el;
}
function parsePicker(html, key) {
  const els = [];
  let m;
  const goRe = /<input[^>]*data-discevgo="([^"]*)"[^>]*>/g;
  while ((m = goRe.exec(html))) {
    const v = /value="([^"]*)"/.exec(m[0]);
    els.push(mkEl("go", {discevgo: m[1]}, {value: v ? v[1] : ""}));
  }
  const fiRe = /<input[^>]*data-discevfilter="([^"]*)"[^>]*>/g;
  while ((m = fiRe.exec(html))) {
    const v = /value="([^"]*)"/.exec(m[0]);
    els.push(mkEl("filter", {discevfilter: m[1]}, {value: v ? v[1] : ""}));
  }
  const cbRe = /<input[^>]*data-discevfile="([^"]*)"[^>]*>/g;
  while ((m = cbRe.exec(html))) {
    els.push(mkEl("file", {discevfile: m[1]},
      {checked: /checked/.test(m[0])}));
  }
  const rmRe = /<button[^>]*data-discevrm="([^"]*)"[^>]*>/g;
  while ((m = rmRe.exec(html))) {
    els.push(mkEl("rm", {discevrm: m[1]}));
  }
  const navRe = /<button[^>]*data-discevnav="([^"]*)"[^>]*>/g;
  while ((m = navRe.exec(html))) {
    els.push(mkEl("nav", {discevnav: m[1]}));
  }
  const dtRe = /<details class="evselmore"( open)?>/g;
  while ((m = dtRe.exec(html))) {
    els.push(mkEl("more", {}, {open: !!m[1]}));
  }
  return els;
}
const boxes = {};
function getBox(key) {
  if (!boxes[key]) {
    const box = {_html: "", els: [],
      _msg: {className: "", textContent: ""}
    };
    Object.defineProperty(box, "innerHTML", {
      get: function() { return this._html; },
      set: function(h) {
        const old = this.els;
        const ae = document.activeElement;
        this._html = h;
        this.els = parsePicker(h, key);
        if (ae && ae !== document.body && old.indexOf(ae) >= 0
            && this.els.indexOf(ae) < 0) {
          document.activeElement = document.body;
        }
      }
    });
    box.contains = function(el) { return this.els.indexOf(el) >= 0; };
    box.querySelector = function(sel) {
      let m = /data-discevmsg="[^"]*"/.exec(sel);
      if (m) { return this._msg; }
      m = /data-discevgo="([^"]+)"/.exec(sel);
      if (m) { return this.els.filter(function(e) {
        return e.dataset.discevgo === m[1]; })[0] || null; }
      m = /data-discevfilter="([^"]+)"/.exec(sel);
      if (m) { return this.els.filter(function(e) {
        return e.dataset.discevfilter === m[1]; })[0] || null; }
      m = /data-discevfile="([^"]+)"/.exec(sel);
      if (m) { return this.els.filter(function(e) {
        return e.dataset.discevfile === m[1]; })[0] || null; }
      m = /data-discevrm="([^"]+)"/.exec(sel);
      if (m) { return this.els.filter(function(e) {
        return e.dataset.discevrm === m[1]; })[0] || null; }
      m = /data-discevselhead="([^"]+)"/.exec(sel);
      if (m) { return this.els.filter(function(e) {
        return e.kind === "head"; })[0] || null; }
      return null;
    };
    box.querySelectorAll = function(sel) {
      if (sel === "[data-discevnav]") {
        return this.els.filter(function(e) { return e.kind === "nav"; });
      }
      if (sel === "[data-discevgo]") {
        return this.els.filter(function(e) { return e.kind === "go"; });
      }
      if (sel === "[data-discevfilter]") {
        return this.els.filter(function(e) { return e.kind === "filter"; });
      }
      if (sel === "[data-discevfile]") {
        return this.els.filter(function(e) { return e.kind === "file"; });
      }
      if (sel === "[data-discevrm]") {
        return this.els.filter(function(e) { return e.kind === "rm"; });
      }
      if (sel === "details.evselmore") {
        return this.els.filter(function(e) { return e.kind === "more"; });
      }
      return [];
    };
    boxes[key] = box;
  }
  return boxes[key];
}
document.querySelector = function(sel) {
  const m = /^\\[data-discevbox="(.+)"\\]$/.exec(sel);
  if (m) { return getBox(m[1]); }
  const s = /^\\[data-discevsel="(.+)"\\]$/.exec(sel);
  if (s) { return {set textContent(v) {}, get textContent() { return ""; }}; }
  return null;
};
document.activeElement = null;
document.body = {};
const gates = {};
jget = function(url) {
  return new Promise(function(res, rej) {
    ((gates[url] = gates[url] || [])).push({res: res, rej: rej});
  });
};
function take(u) { return gates[u].shift(); }
const tick = function() { return new Promise(function(r) {
  setTimeout(r, 5); }); };
const R = {name: "r"};
const uRoot = "/api/discussions/sources?repo=r&dir=";
const uSub = "/api/discussions/sources?repo=r&dir=sub";
(async function() {
  const rep = {};
  const p0 = discEvidence(R, "k", "");
  take(uRoot).res({dir: "", dirs: ["sub"]
    , files: [{name: "top.py", size: 3}]});
  await p0;
  await tick();
  const box = boxes.k;
  rep.opened = box.innerHTML.indexOf("근거 파일 고르기") >= 0;
  let go = box.querySelector('[data-discevgo="k"]');
  rep.goUsable = !!go;
  go.value = "sub";
  go.oninput();
  rep.draftKept = discEvGo.k === "sub";
  document.activeElement = go;
  go.onkeydown({key: "Enter", preventDefault: function() {}});
  take(uSub).res({dir: "sub/", dirs: []
    , files: [{name: "deep.py", size: 7}]});
  await tick();
  await tick();
  go = box.querySelector('[data-discevgo="k"]');
  rep.goValue = go && go.value;
  rep.crumb = box.innerHTML.indexOf('data-discevnav="k|sub"') >= 0;
  rep.focusGo = trail.some(function(t) {
    return t[0] === "focus" && t[1] === "go"; });
  const fi = box.querySelector('[data-discevfilter="k"]');
  fi.value = "dee";
  fi.selectionStart = 1;
  fi.selectionEnd = 3;
  document.activeElement = fi;
  fi.oninput();
  rep.caret = trail.some(function(t) {
    return t[0] === "sel" && t[1] === "filter" && t[2] === 1
      && t[3] === 3; });
  rep.filtered = box.innerHTML.indexOf("deep.py") >= 0;
  const cb = box.querySelector('[data-discevfile="k|sub/deep.py"]');
  rep.cbFound = !!cb;
  document.activeElement = cb;
  cb.checked = true;
  cb.onchange();
  rep.selCount = discEvSel.k.length;
  rep.headShown = box.innerHTML.indexOf(
    "다음 검토에 포함할 파일 1건(아직 미실행)") >= 0;
  rep.sizeShown = box.innerHTML.indexOf("sub/deep.py") >= 0
    && box.innerHTML.indexOf("7B") >= 0;
  rep.noRatio = box.innerHTML.indexOf("20000/") < 0;
  rep.focusCb = trail.some(function(t) {
    return t[0] === "focus" && t[1] === "file"; });
  document.activeElement = {tagName: "DIV"};
  const nFocus = trail.length;
  fi2keep = box.querySelector('[data-discevfilter="k"]');
  const fi2 = fi2keep;
  fi2.value = "deep";
  fi2.oninput();
  rep.noSteal = trail.length === nFocus;
  rep.stillFilters = box.innerHTML.indexOf("deep.py") >= 0;
  const rms = box.querySelectorAll('[data-discevrm]');
  rep.rmFound = rms.length === 1;
  document.activeElement = document.body;
  const nRmTrail = trail.length;
  rms[0].onclick();
  rep.selEmpty = discEvSel.k.length === 0;
  rep.fallbackFilter = trail.slice(nRmTrail).some(function(t) {
    return t[0] === "focus" && t[1] === "filter"; });
  console.log(JSON.stringify(rep));
})();
""")
        got = out[-1]
        self.assertTrue(got["opened"], got)
        self.assertTrue(got["goUsable"], got)
        self.assertTrue(got["draftKept"], got)
        self.assertEqual(got["goValue"], "sub", got)
        self.assertTrue(got["crumb"], got)
        self.assertTrue(got["focusGo"], got)
        self.assertTrue(got["caret"], got)
        self.assertTrue(got["filtered"], got)
        self.assertTrue(got["cbFound"], got)
        self.assertEqual(got["selCount"], 1, got)
        self.assertTrue(got["headShown"], got)
        self.assertTrue(got["sizeShown"], got)
        self.assertTrue(got["noRatio"], got)
        self.assertTrue(got["focusCb"], got)
        self.assertTrue(got["noSteal"], got)
        self.assertTrue(got["stillFilters"], got)
        self.assertTrue(got["rmFound"], got)
        self.assertTrue(got["selEmpty"], got)
        self.assertTrue(got["fallbackFilter"], got)

    def test_reconcile_refreshes_edited_unreviewed_node_only(self):
        # Actual discReconcileOpen with scripted jget: an unreviewed
        # selected node whose list row carries newer updated_at/title
        # is refetched (cached GET, no model call); a reviewed-identical
        # selection stays quiet while a running-job selection is still
        # polled for its transient state; selection state survives.
        out = run_driver("""
const calls = [];
const nodeResp = {
  1: {node: {number: 1, title: "rev2", body: "b2", updated_at: "t2"}
      , review: {status: "unreviewed", stale: false
                 , job: {status: "idle"}}},
  4: {node: {number: 4, title: "rev2", body: "nb2", updated_at: "t2"}
      , review: {status: "stale", stale: true
                 , job: {status: "idle"}
                 , report: {summary: "S"}}},
  5: {node: {number: 5, title: "old", body: "b", updated_at: "t0"}
      , review: {status: "unreviewed", stale: false
                 , job: {status: "idle"}}}
};
jget = (url) => {
  calls.push(url);
  const m = /number=(\\d+)/.exec(url);
  const n = m && m[1];
  return Promise.resolve(nodeResp[n] || nodeResp[1]);
};
const R = {name: "r"};
const rev1 = function() {
  return {node: {number: 1, title: "rev1", body: "b1",
                 updated_at: "t1"}
          , review: {status: "unreviewed", stale: false
                     , job: {status: "idle"}}};
};
const row1rev2 = function() {
  return {number: 1, title: "rev2", updated_at: "t2"
          , review: {status: "unreviewed", stale: false
                     , job_status: "idle"}};
};
const nodeHits = function(n) {
  return calls.filter(function(u) {
    return u.indexOf("/api/discussions/node") >= 0
      && u.indexOf("number=" + n) >= 0; }).length;
};
(async () => {
  const rep = {};
  discSel["r"] = "r:1";
  discOpen.add("r:1");
  discEvSel["r:1"] = ["src/a.py"];
  discEvGo["r:1"] = "src";
  discNodes["r:1"] = rev1();
  await discReconcileOpen(R, {items: [row1rev2()]});
  rep.fetched = nodeHits(1);
  rep.title = discNodes["r:1"].node.title;
  rep.body = discNodes["r:1"].node.body;
  rep.selKept = discEvSel["r:1"];
  rep.goKept = discEvGo["r:1"];
  const nCalls = calls.length;
  discSel["r"] = "r:2";
  discOpen.add("r:2");
  discNodes["r:2"] = {node: {number: 2, title: "same"
                             , body: "b", updated_at: "t0"}
                      , review: {status: "reviewed", stale: false
                                 , job: {status: "idle"}}};
  await discReconcileOpen(R, {items: [{number: 2, title: "same"
    , updated_at: "t0"
    , review: {status: "reviewed", stale: false, job_status: "idle"}}]});
  rep.reviewedQuiet = calls.length === nCalls;
  discSel["r"] = "r:3";
  discOpen.add("r:3");
  discNodes["r:3"] = {node: {number: 3, title: "old"
                             , body: "b", updated_at: "t0"}
                      , review: {status: "unreviewed", stale: false
                                 , job: {status: "running"}}};
  await discReconcileOpen(R, {items: [{number: 3, title: "new"
    , updated_at: "t9"
    , review: {status: "unreviewed", stale: false
               , job_status: "running"}}]});
  rep.runningPolled = calls.length > nCalls;
  discSel["r"] = "r:4";
  discOpen.add("r:4");
  discNodes["r:4"] = {node: {number: 4, title: "rev1", body: "nb1"
                             , updated_at: "t1"}
                      , review: {status: "stale", stale: true
                                 , job: {status: "idle"}
                                 , report: {summary: "S"}}};
  await discReconcileOpen(R, {items: [{number: 4, title: "rev2"
    , updated_at: "t2"
    , review: {status: "stale", stale: true, job_status: "idle"}}]});
  rep.staleFetched = nodeHits(4) === 1;
  rep.staleTitle = discNodes["r:4"].node.title;
  rep.reportKept = discNodes["r:4"].review.report
    && discNodes["r:4"].review.report.summary;
  discSel["r"] = "r:5";
  discOpen.add("r:5");
  discNodes["r:5"] = {node: {number: 5, title: "old", body: "b"
                             , updated_at: "t0"}
                      , review: {status: "unreviewed", stale: false
                                 , job: {status: "idle"}}};
  const row5 = {number: 5, title: "rev2", updated_at: "t2"
    , review: {status: "unreviewed", stale: false, job_status: "idle"}};
  await discReconcileOpen(R, {items: [row5]});
  rep.onceObserved = nodeHits(5) === 1;
  await discReconcileOpen(R, {items: [row5]});
  rep.noRepeat = nodeHits(5) === 1;
  await discReconcileOpen(R, {items: [{number: 5, title: "rev3"
    , updated_at: "t3"
    , review: {status: "unreviewed", stale: false
               , job_status: "idle"}}]});
  rep.newRevRefetch = nodeHits(5) === 2;
  discNodes["r:9"] = {node: {number: 9, title: "old", body: "b"
                             , updated_at: "t0"}
                      , review: {status: "unreviewed", stale: false
                                 , job: {status: "idle"}}};
  discOpen.add("r:9");
  await discReconcileOpen(R, {items: [{number: 9, title: "rev9"
    , updated_at: "t9"
    , review: {status: "unreviewed", stale: false
               , job_status: "idle"}}]});
  rep.unopenedQuiet = nodeHits(9) === 0;
  console.log(JSON.stringify(rep));
})();
""")
        got = out[-1]
        self.assertEqual(got["fetched"], 1, got)
        self.assertEqual(got["title"], "rev2", got)
        self.assertEqual(got["body"], "b2", got)
        self.assertEqual(got["selKept"], ["src/a.py"], got)
        self.assertEqual(got["goKept"], "src", got)
        self.assertTrue(got["reviewedQuiet"], got)
        self.assertTrue(got["runningPolled"], got)
        self.assertTrue(got["staleFetched"], got)
        self.assertEqual(got["staleTitle"], "rev2", got)
        self.assertEqual(got["reportKept"], "S", got)
        self.assertTrue(got["onceObserved"], got)
        self.assertTrue(got["noRepeat"], got)
        self.assertTrue(got["newRevRefetch"], got)
        self.assertTrue(got["unopenedQuiet"], got)

    def test_evidence_panel_survives_outer_redraw(self):
        # Failed/loading picker must survive an outer card re-render:
        # draft, selection and the error stay visible (no invented
        # listing, no re-query), and a corrected navigation clears the
        # error. Actual production render + wire functions throughout.
        out = run_driver("""
LIST = %s;
NODE = %s;
SOURCES = {repo: "r", slug: "o/r", dir: ""
  , dirs: ["src"], files: [{name: "a.py", size: 20}]
  , truncated: false};
const _jget = jget;
jget = (url) => url.indexOf("dir=bad") >= 0
  ? Promise.reject(new Error("nope"))
  : _jget(url);
const R = {name: "r"};
(async () => {
  const rep = {};
  await paintRepoCommunity(R);
  await discToggleDetail(R, "r:1", LIST.catalog);
  discEvSel["r:1"] = ["src/a.py"];
  discEvGo["r:1"] = "src";
  await discEvidence(R, "r:1", "bad");
  rep.errShown = combinedHtml().indexOf("목록 조회 실패") >= 0;
  rep.draftShown = combinedHtml().indexOf('value="src"') >= 0;
  rep.selShown = combinedHtml().indexOf("src/a.py") >= 0;
  await paintRepoCommunity(R);
  rep.errKept = combinedHtml().indexOf("목록 조회 실패") >= 0;
  rep.draftKept = combinedHtml().indexOf('value="src"') >= 0;
  rep.selKept = combinedHtml().indexOf("src/a.py") >= 0;
  await discEvidence(R, "r:1", "");
  await paintRepoCommunity(R);
  rep.errCleared = combinedHtml().indexOf("목록 조회 실패") < 0;
  rep.listShown = combinedHtml().indexOf("a.py") >= 0;
  console.log(JSON.stringify(rep));
})();
""" % (json.dumps(server_shaped_list()),
       json.dumps(server_shaped_node())),
            prelude_extra=review_state_js() + POLL_FAKE_JS)
        got = out[-1]
        self.assertTrue(got["errShown"], got)
        self.assertTrue(got["draftShown"], got)
        self.assertTrue(got["selShown"], got)
        self.assertTrue(got["errKept"], got)
        self.assertTrue(got["draftKept"], got)
        self.assertTrue(got["selKept"], got)
        self.assertTrue(got["errCleared"], got)
        self.assertTrue(got["listShown"], got)

    def test_fp_title_and_selected_node_rev_change_identity(self):
        # Paint identity covers list titles and the selected cached
        # node revision: a title-only change (or a node refresh with
        # the same list bytes) must not hit the equal-fingerprint
        # early return. No volatile timestamps in the identity.
        out = run_driver("""
const L = %s;
const N = %s;
const fp0 = discFp("r", L, false, null);
const fp0b = discFp("r", L, false, null);
L.items[0].title = "New title";
const fpTitle = discFp("r", L, false, null);
discSel["r"] = "r:1";
discOpen.add("r:1");
discNodes["r:1"] = N;
const fpSel = discFp("r", L, false, null);
discNodes["r:1"] = {node: {number: 1, title: "Newer", body: "b"
  , updated_at: "t9"}, review: N.review};
const fpSelNew = discFp("r", L, false, null);
console.log(JSON.stringify({stable: fp0 === fp0b
  , titleMoves: fp0 !== fpTitle, selMoves: fpTitle !== fpSel
  , nodeMoves: fpSel !== fpSelNew}));
""" % (json.dumps(server_shaped_list()),
       json.dumps(server_shaped_node())))
        got = out[-1]
        self.assertTrue(got["stable"], got)
        self.assertTrue(got["titleMoves"], got)
        self.assertTrue(got["selMoves"], got)
        self.assertTrue(got["nodeMoves"], got)

    def test_title_only_change_repaints_inspector(self):
        # Actual paint path: a title-only list change (authoritative
        # timestamp held unchanged) for the selected node refetches the
        # cached node and repaints the inspector (row + title + body),
        # with no stale-note left over.
        out = run_driver("""
LIST = %s;
NODE = %s;
const _jget0 = jget;
const stamp0 = LIST.items[0].updated_at;
let nodeTitle = LIST.items[0].title;
let nodeBody = "body-rev1";
jget = (url) => {
  if (url.indexOf("/api/discussions/node") >= 0) {
    return Promise.resolve({node: {number: 1, title: nodeTitle
      , body: nodeBody, updated_at: stamp0}
      , review: {status: "unreviewed", stale: false
                 , job: {status: "idle"}}});
  }
  return _jget0(url);
};
const R = {name: "r"};
(async () => {
  const rep = {};
  await paintRepoCommunity(R);
  discSel["r"] = "r:1";
  discOpen.add("r:1");
  discNodes["r:1"] = {node: {number: 1, title: LIST.items[0].title
    , body: "body-rev1", updated_at: stamp0}
    , review: {status: "unreviewed", stale: false
               , job: {status: "idle"}}};
  await paintRepoCommunity(R);
  rep.beforeOld = tabEl._html.indexOf("body-rev1") >= 0;
  LIST.items[0].title = "rev2";
  nodeTitle = "rev2";
  nodeBody = "body-rev2";
  delete discCache["r"];
  await paintRepoCommunity(R);
  const after = tabEl._html;
  rep.redrawn = after.indexOf("body-rev1") < 0;
  rep.inspTitle = after.indexOf("rev2") >= 0;
  rep.inspBody = after.indexOf("body-rev2") >= 0;
  rep.noteGone = after.indexOf("보관된 원문이 목록보다 오래됨") < 0
    && after.indexOf("원문과 목록이 다름") < 0;
  console.log(JSON.stringify(rep));
})();
""" % (json.dumps(server_shaped_list()),
       json.dumps(server_shaped_node())),
            prelude_extra=review_state_js() + POLL_FAKE_JS)
        got = out[-1]
        self.assertTrue(got["beforeOld"], got)
        self.assertTrue(got["redrawn"], got)
        self.assertTrue(got["inspTitle"], got)
        self.assertTrue(got["inspBody"], got)
        self.assertTrue(got["noteGone"], got)

    def test_stale_node_notice_and_manual_refetch(self):
        # Cached node older than the list shows a plain notice with an
        # explicit model-free refetch control. Polling never POSTs;
        # only the click fetches, and the saved report stays rendered.
        out = run_driver("""
LIST = %s;
NODE = %s;
const _jget0 = jget;
const stamps7 = {"rev1": "2026-10-26T00:00:00+00:00"
  , "rev2": "2026-10-26T00:05:00+00:00"};
let nodeNow = "rev1";
jget = (url) => {
  if (url.indexOf("/api/discussions/node") >= 0) {
    return Promise.resolve({node: {number: 1, title: nodeNow
      , body: "b-" + nodeNow, updated_at: stamps7[nodeNow]}
      , review: {status: "unreviewed", stale: false
                 , job: {status: "idle"}
                 , report: {summary: "SEED-REPORT-7", findings: []}}});
  }
  return _jget0(url);
};
const R = {name: "r"};
(async () => {
  const rep = {};
  LIST.items[0].title = "rev2";
  LIST.items[0].updated_at = "2026-10-26T00:05:00+00:00";
  await paintRepoCommunity(R);
  discSel["r"] = "r:1";
  discOpen.add("r:1");
  discNodes["r:1"] = {node: {number: 1, title: "rev1", body: "b-rev1"
    , updated_at: "2026-10-26T00:00:00+00:00"}
    , review: {status: "unreviewed", stale: false
               , job: {status: "idle"}
               , report: {summary: "SEED-REPORT-7", findings: []}}};
  delete discCache["r"];
  await paintRepoCommunity(R);
  rep.notice = tabEl._html.indexOf("보관된 원문이 목록보다 오래됨") >= 0;
  rep.hasBtn = tabEl._html.indexOf("data-discnodefetch") >= 0;
  rep.noAutoPost = !postBodies["/api/discussions/detail"];
  await discReconcileOpen(R, LIST);
  await paintRepoCommunity(R);
  rep.stillNoPost = !postBodies["/api/discussions/detail"];
  rep.nodeStillOld = discNodes["r:1"].node.title === "rev1";
  nodeNow = "rev2";
  await discFetchNode(R, "r:1"
    , {disabled: false, textContent: "원문 새로 가져오기"});
  rep.postMade = !!postBodies["/api/discussions/detail"];
  const after = tabEl._html;
  rep.noteGone = after.indexOf("보관된 원문이 목록보다 오래됨") < 0;
  rep.bodyNew = after.indexOf("b-rev2") >= 0;
  rep.reportKept = after.indexOf("SEED-REPORT-7") >= 0;
  rep.okToast = toasts.indexOf("원문을 새로 가져왔습니다") >= 0;
  console.log(JSON.stringify(rep));
})();
""" % (json.dumps(server_shaped_list()),
       json.dumps(server_shaped_node())),
            prelude_extra=review_state_js() + POLL_FAKE_JS)
        got = out[-1]
        self.assertTrue(got["notice"], got)
        self.assertTrue(got["hasBtn"], got)
        self.assertTrue(got["noAutoPost"], got)
        self.assertTrue(got["stillNoPost"], got)
        self.assertTrue(got["nodeStillOld"], got)
        self.assertTrue(got["postMade"], got)
        self.assertTrue(got["noteGone"], got)
        self.assertTrue(got["bodyNew"], got)
        self.assertTrue(got["reportKept"], got)
        self.assertTrue(got["okToast"], got)

    def test_manual_refetch_failure_and_dedup(self):
        # Failed refetch restores a usable control and never toasts
        # success; a wrong-number payload is rejected; two concurrent
        # clicks cause a single POST per selected key.
        out = run_driver("""
LIST = %s;
NODE = %s;
const _jget0 = jget;
const _jpost0 = jpost;
const R = {name: "r"};
const rev1node = function() {
  return {node: {number: 1, title: "rev1", body: "b-rev1"
    , updated_at: "t-rev1"}
    , review: {status: "unreviewed", stale: false
               , job: {status: "idle"}}};
};
(async () => {
  const rep = {};
  discNodes["r:1"] = rev1node();
  jpost = (url, body) => url === "/api/discussions/detail"
    ? Promise.reject(new Error("worker busy"))
    : _jpost0(url, body);
  const btn = {disabled: false, textContent: "원문 새로 가져오기"};
  await discFetchNode(R, "r:1", btn);
  rep.btnUsable = btn.disabled === false
    && btn.textContent === "원문 새로 가져오기";
  rep.failToast = toasts.some(function(t) {
    return t.indexOf("원문 가져오기 실패") >= 0; });
  rep.noOkToast = toasts.indexOf("원문을 새로 가져왔습니다") < 0;
  rep.nodeKept = discNodes["r:1"].node.title === "rev1";
  jpost = _jpost0;
  jget = (url) => url.indexOf("/api/discussions/node") >= 0
    ? Promise.resolve({node: {number: 999, title: "wrong", body: "w"
      , updated_at: "t9"}
      , review: {status: "unreviewed", stale: false
                 , job: {status: "idle"}}})
    : _jget0(url);
  const okCount = toasts.length;
  await discFetchNode(R, "r:1", btn);
  rep.wrongRejected = discNodes["r:1"].node.title === "rev1"
    && toasts.length === okCount + 1
    && toasts[okCount].indexOf("원문 가져오기 실패") >= 0;
  jget = _jget0;
  let postCount = 0;
  let releasePost = null;
  jpost = (url, body) => {
    if (url === "/api/discussions/detail") {
      postCount++;
      return new Promise(function(res) {
        releasePost = function() { res({ok: true}); };
      });
    }
    return _jpost0(url, body);
  };
  const p1 = discFetchNode(R, "r:1", null);
  const p2 = discFetchNode(R, "r:1", null);
  await new Promise(function(res) { setTimeout(res, 20); });
  rep.dedupSingle = postCount === 1;
  releasePost();
  await p1;
  await p2;
  rep.dedupNode = discNodes["r:1"].node.number === 1;
  console.log(JSON.stringify(rep));
})();
""" % (json.dumps(server_shaped_list()),
       json.dumps(server_shaped_node())),
            prelude_extra=review_state_js() + POLL_FAKE_JS)
        got = out[-1]
        self.assertTrue(got["btnUsable"], got)
        self.assertTrue(got["failToast"], got)
        self.assertTrue(got["noOkToast"], got)
        self.assertTrue(got["nodeKept"], got)
        self.assertTrue(got["wrongRejected"], got)
        self.assertTrue(got["dedupSingle"], got)
        self.assertTrue(got["dedupNode"], got)

    def test_manual_refetch_navigate_away_and_inflight(self):
        # A deferred refetch renders its in-flight busy control through
        # an actual outer redraw; if the reader navigates to another
        # tab before completion, the valid node is cached but the old
        # repo screen never paints into the new tab and no toast fires.
        out = run_driver("""
LIST = %s;
NODE = %s;
const _jget0 = jget;
const _jpost0 = jpost;
view = {id: "work"};
repoTab = "community";
const R = {name: "r"};
const revG = {node: {number: 1, title: "rev1", body: "b-rev1"
  , updated_at: "2026-10-26T00:00:00+00:00"}
  , review: {status: "unreviewed", stale: false
             , job: {status: "idle"}}};
let releasePost = null;
jpost = (url, body) => url === "/api/discussions/detail"
  ? new Promise(function(res) {
      releasePost = function() { res({ok: true}); };
    })
  : _jpost0(url, body);
jget = (url) => url.indexOf("/api/discussions/node") >= 0
  ? Promise.resolve(JSON.parse(JSON.stringify(revG)))
  : _jget0(url);
(async () => {
  const rep = {};
  LIST.items[0].title = "rev2";
  LIST.items[0].updated_at = "2026-10-26T00:05:00+00:00";
  await paintRepoCommunity(R);
  discSel["r"] = "r:1";
  discOpen.add("r:1");
  discNodes["r:1"] = JSON.parse(JSON.stringify(revG));
  delete discCache["r"];
  await paintRepoCommunity(R);
  rep.notice = tabEl._html.indexOf("보관된 원문이 목록보다 오래됨") >= 0;
  const btn = {disabled: false, textContent: "원문 새로 가져오기"};
  const p = discFetchNode(R, "r:1", btn);
  await new Promise(function(res) { setTimeout(res, 20); });
  rep.btnBusy = btn.disabled === true;
  await paintRepoCommunity(R);
  const mid = tabEl._html;
  rep.inflightBusy = mid.indexOf("가져오는 중") >= 0
    && mid.indexOf("data-discnodefetch") >= 0;
  const htmlBeforeLeave = tabEl._html;
  repoTab = "reviews";
  revG.node.title = "rev2";
  revG.node.body = "b-rev2";
  revG.node.updated_at = "2026-10-26T00:05:00+00:00";
  releasePost();
  await p;
  rep.cached = discNodes["r:1"].node.title === "rev2";
  rep.noForeignPaint = tabEl._html === htmlBeforeLeave;
  rep.noOkToast = toasts.indexOf("원문을 새로 가져왔습니다") < 0;
  console.log(JSON.stringify(rep));
})();
""" % (json.dumps(server_shaped_list()),
       json.dumps(server_shaped_node())),
            prelude_extra=review_state_js() + POLL_FAKE_JS)
        got = out[-1]
        self.assertTrue(got["notice"], got)
        self.assertTrue(got["btnBusy"], got)
        self.assertTrue(got["inflightBusy"], got)
        self.assertTrue(got["cached"], got)
        self.assertTrue(got["noForeignPaint"], got)
        self.assertTrue(got["noOkToast"], got)

    def test_newer_node_and_unknown_date_copy(self):
        # A cached node newer than the list is never called old and is
        # fetched at most once per list revision; an unknown order gets
        # a neutral mismatch copy with a manual refetch control.
        out = run_driver("""
LIST = %s;
NODE = %s;
const _jget0 = jget;
let nodeHitsH = 0;
jget = (url) => {
  const m = /number=(\\d+)/.exec(url);
  if (url.indexOf("/api/discussions/node") >= 0 && m) {
    nodeHitsH++;
    if (m[1] === "858") {
      return Promise.resolve({node: {number: 858, title: "q2-node"
        , body: "qb"}
        , review: {status: "unreviewed", stale: false
                   , job: {status: "idle"}}});
    }
    return Promise.resolve({node: {number: 1, title: "rev2"
      , body: "b-new", updated_at: "2026-10-26T00:10:00+00:00"}
      , review: {status: "unreviewed", stale: false
                 , job: {status: "idle"}}});
  }
  return _jget0(url);
};
const R = {name: "r"};
(async () => {
  const rep = {};
  LIST.items[0].title = "rev1";
  LIST.items[0].updated_at = "2026-10-26T00:00:00+00:00";
  LIST.items[0].review.status = "unreviewed";
  LIST.items[1].title = "q2-list";
  await paintRepoCommunity(R);
  discSel["r"] = "r:1";
  discOpen.add("r:1");
  discNodes["r:1"] = {node: {number: 1, title: "rev2", body: "b-new"
    , updated_at: "2026-10-26T00:10:00+00:00"}
    , review: {status: "unreviewed", stale: false
               , job: {status: "idle"}}};
  delete discCache["r"];
  await paintRepoCommunity(R);
  const newer = tabEl._html;
  rep.newerCopy = newer.indexOf("목록이 원문보다 오래됨") >= 0;
  rep.noOlderClaim = newer.indexOf("보관된 원문이 목록보다 오래됨") < 0;
  rep.noBtn = newer.indexOf("data-discnodefetch") < 0;
  const callsAfterPaint = nodeHitsH;
  await discReconcileOpen(R, LIST);
  await discReconcileOpen(R, LIST);
  rep.singleFetch = callsAfterPaint === 1 && nodeHitsH === 1;
  discSel["r"] = "r:858";
  discOpen.add("r:858");
  discNodes["r:858"] = {node: {number: 858, title: "q2-old", body: "qb"
    , updated_at: "2026-10-24T00:00:00+00:00"}
    , review: {status: "unreviewed", stale: false
               , job: {status: "idle"}}};
  delete discCache["r"];
  await paintRepoCommunity(R);
  const unk = tabEl._html;
  rep.neutralCopy = unk.indexOf("원문과 목록이 다름") >= 0;
  rep.hasBtn = unk.indexOf("data-discnodefetch") >= 0;
  rep.noOlderClaim2 = unk.indexOf("보관된 원문이 목록보다 오래됨") < 0;
  console.log(JSON.stringify(rep));
})();
""" % (json.dumps(server_shaped_list()),
       json.dumps(server_shaped_node())),
            prelude_extra=review_state_js() + POLL_FAKE_JS)
        got = out[-1]
        self.assertTrue(got["newerCopy"], got)
        self.assertTrue(got["noOlderClaim"], got)
        self.assertTrue(got["noBtn"], got)
        self.assertTrue(got["singleFetch"], got)
        self.assertTrue(got["neutralCopy"], got)
        self.assertTrue(got["hasBtn"], got)
        self.assertTrue(got["noOlderClaim2"], got)

    def test_reconcile_failed_fetch_retries_next_poll(self):
        # A rejected cached node fetch records no list revision, so the
        # next poll retries instead of going quiet forever.
        out = run_driver("""
const _jget0 = jget;
let nodeFail = true;
let fetchTries5 = 0;
jget = (url) => {
  if (url.indexOf("/api/discussions/node") >= 0) {
    fetchTries5++;
    return nodeFail ? Promise.reject(new Error("net"))
      : Promise.resolve({node: {number: 5, title: "rev2", body: "b2"
        , updated_at: "t2"}
        , review: {status: "unreviewed", stale: false
                   , job: {status: "idle"}}});
  }
  return _jget0(url);
};
const R = {name: "r"};
const row5 = {number: 5, title: "rev2", updated_at: "t2"
  , review: {status: "unreviewed", stale: false, job_status: "idle"}};
(async () => {
  const rep = {};
  discSel["r"] = "r:5";
  discOpen.add("r:5");
  discNodes["r:5"] = {node: {number: 5, title: "old", body: "b"
    , updated_at: "t0"}
    , review: {status: "unreviewed", stale: false
               , job: {status: "idle"}}};
  await discReconcileOpen(R, {items: [row5]});
  rep.tried = fetchTries5 === 1;
  rep.keptOld = discNodes["r:5"].node.title === "old";
  nodeFail = false;
  await discReconcileOpen(R, {items: [row5]});
  rep.retried = fetchTries5 === 2;
  rep.updated = discNodes["r:5"].node.title === "rev2";
  console.log(JSON.stringify(rep));
})();
""")
        got = out[-1]
        self.assertTrue(got["tried"], got)
        self.assertTrue(got["keptOld"], got)
        self.assertTrue(got["retried"], got)
        self.assertTrue(got["updated"], got)

    def test_evidence_refresh_state_beats_old_cache(self):
        # Newest pending/error wins over a prior successful directory
        # cache in every redraw path (outer card repaint and the shared
        # filter/remove rerender), with an actual repaint verified —
        # not a fingerprint no-op. Correction clears the state.
        out = run_driver("""
LIST = %s;
NODE = %s;
SOURCES = {repo: "r", slug: "o/r", dir: ""
  , dirs: ["src"], files: [{name: "a.py", size: 20}
    , {name: "zzz-unselected.py", size: 7}]
  , truncated: false};
const SLOW = {repo: "r", slug: "o/r", dir: "slow/"
  , dirs: [], files: [{name: "s.py", size: 6}]
  , truncated: false};
const _jget0 = jget;
let failSrc = false;
let slowRes = null;
jget = (url) => {
  if (url.indexOf("dir=slow") >= 0) {
    return new Promise(function(res) {
      slowRes = function() {
        res(JSON.parse(JSON.stringify(SLOW))); };
    });
  }
  if (url.indexOf("/api/discussions/sources") >= 0 && failSrc) {
    return Promise.reject(new Error("boom"));
  }
  return _jget0(url);
};
const R = {name: "r"};
(async () => {
  const rep = {};
  await paintRepoCommunity(R);
  await discToggleDetail(R, "r:1", LIST.catalog);
  discEvSel["r:1"] = ["a.py"];
  discEvGo["r:1"] = "src";
  await discEvidence(R, "r:1", "");
  failSrc = true;
  await discEvidence(R, "r:1", "");
  rep.errShown = combinedHtml().indexOf("목록 조회 실패") >= 0;
  const before = combinedHtml();
  discFPlatform = "android";
  await paintRepoCommunity(R);
  const mid = combinedHtml();
  rep.redrawn = mid !== before;
  rep.errKept = mid.indexOf("목록 조회 실패") >= 0;
  rep.priorNote = mid.indexOf("보관 중") >= 0;
  rep.priorHidden = mid.indexOf("zzz-unselected.py") < 0;
  rep.draftKept = mid.indexOf('value="src"') >= 0;
  rep.selKept = mid.indexOf("a.py") >= 0;
  discEvidenceRerender(R, "r:1");
  rep.rerenderKeepsErr = combinedHtml().indexOf("목록 조회 실패") >= 0;
  rep.rerenderPrior = combinedHtml().indexOf("보관 중") >= 0;
  failSrc = false;
  await discEvidence(R, "r:1", "");
  discFPlatform = "";
  await paintRepoCommunity(R);
  const after = combinedHtml();
  rep.errCleared = after.indexOf("목록 조회 실패") < 0;
  rep.priorGone = after.indexOf("보관 중") < 0;
  rep.listShown = after.indexOf("a.py") >= 0;
  const lb = combinedHtml();
  const p = discEvidence(R, "r:1", "slow");
  discFPlatform = "ios";
  await paintRepoCommunity(R);
  const lmid = combinedHtml();
  rep.loadRedrawn = lmid !== lb;
  rep.loadingShown = lmid.indexOf("목록 불러오는 중") >= 0;
  discFPlatform = "";
  slowRes();
  await p;
  await paintRepoCommunity(R);
  const done = combinedHtml();
  rep.completedList = done.indexOf("s.py") >= 0;
  rep.loadingGone = done.indexOf("목록 불러오는 중") < 0;
  console.log(JSON.stringify(rep));
})();
""" % (json.dumps(server_shaped_list()),
       json.dumps(server_shaped_node())),
            prelude_extra=review_state_js() + POLL_FAKE_JS)
        got = out[-1]
        self.assertTrue(got["errShown"], got)
        self.assertTrue(got["redrawn"], got)
        self.assertTrue(got["errKept"], got)
        self.assertTrue(got["priorNote"], got)
        self.assertTrue(got["priorHidden"], got)
        self.assertTrue(got["draftKept"], got)
        self.assertTrue(got["selKept"], got)
        self.assertTrue(got["rerenderKeepsErr"], got)
        self.assertTrue(got["rerenderPrior"], got)
        self.assertTrue(got["errCleared"], got)
        self.assertTrue(got["priorGone"], got)
        self.assertTrue(got["listShown"], got)
        self.assertTrue(got["loadRedrawn"], got)
        self.assertTrue(got["loadingShown"], got)
        self.assertTrue(got["completedList"], got)
        self.assertTrue(got["loadingGone"], got)

    def test_review_state_caret_roundtrip(self):
        # Real production capture/restore helpers (not the prelude
        # stubs) via the existing review_state_js() extraction.
        out = run_driver("""
function scrollerOf(box) { return null; }
const calls = [];
const fakeEl = {
  focus(o) { calls.push(["focus", o && o.preventScroll]); },
  setSelectionRange(a, b) { calls.push(["sel", a, b]); },
};
const fakeBox = {dataset: {},
  querySelector(sel) { return fakeEl; }
  , querySelectorAll(sel) { return []; }
  , parentElement: null};
document.querySelector = (sel) => null;
document.activeElement = null;
document.body = {};
document.scrollingElement = null;
document.documentElement = null;
restoreReviewState(fakeBox, {key: "k", sel: [2, 5]});
const n1 = calls.length;
document.activeElement = {tagName: "DIV"};
restoreReviewState(fakeBox, {key: "k", sel: [0, 1]});
const capBox = {dataset: {}
  , querySelector(sel) { return null; }
  , querySelectorAll(sel) { return []; }
  , parentElement: null};
const fakeInput = {dataset: {k: "discevgo:k", discevgo: "k"}
  , selectionStart: 3, selectionEnd: 7};
document.activeElement = fakeInput;
const st = captureReviewState(capBox);
console.log(JSON.stringify({calls, n1, stKey: st.key, stSel: st.sel}));
""", prelude_extra=review_state_js())
        got = out[-1]
        self.assertEqual(got["calls"], [["focus", True],
                                       ["sel", 2, 5]])
        self.assertEqual(got["n1"], 2)
        self.assertEqual(got["stKey"], "discevgo:k")
        self.assertEqual(got["stSel"], [3, 7])

    def test_evidence_picker_request_race(self):
        # A1 then B: B completes latest, so a late A1 error must not
        # overwrite B. A1, B then A2: only the newest A2 paints; stale
        # B/A1 results and errors are discarded. Uncontrolled network
        # is never used: every completion order is scripted.
        out = run_driver("""
const boxes = {};
function getBox(k) {
  if (!boxes[k]) boxes[k] = {
    innerHTML: ""
    , contains(el) { return false; }
    , querySelector(sel) {
      if (sel.indexOf("discevmsg") >= 0) return {className: ""
        , textContent: ""};
      return null;
    }
    , querySelectorAll(sel) { return []; }
  };
  return boxes[k];
}
document.querySelector = (sel) => {
  const m = /^\\[data-discevbox="(.+)"\\]$/.exec(sel);
  return m ? getBox(m[1]) : null;
};
document.activeElement = null;
document.body = {};
const gates = {};
const take = (u) => gates[u].shift();
jget = (url) => new Promise((res, rej) => {
  (gates[url] = gates[url] || []).push({res, rej});
});
const tick = () => new Promise((r) => setTimeout(r, 5));
const R = {name: "r"};
const uA = "/api/discussions/sources?repo=r&dir=A";
const uB = "/api/discussions/sources?repo=r&dir=B";
(async () => {
  const p1 = discEvidence(R, "s1", "A");
  const p2 = discEvidence(R, "s1", "B");
  take(uB).res({dir: "B/", dirs: []
    , files: [{name: "b.py", size: 1}]});
  await tick(); await tick();
  const sawB = boxes.s1.innerHTML;
  take(uA).rej(new Error("slow A"));
  await p1; await p2;
  await tick();
  const finB = boxes.s1.innerHTML;
  const q1 = discEvidence(R, "s2", "A");
  const q2 = discEvidence(R, "s2", "B");
  const q3 = discEvidence(R, "s2", "A");
  take(uA).res({dir: "A/", dirs: []
    , files: [{name: "stale.py", size: 1}]});
  await tick();
  const midStale = boxes.s2.innerHTML;
  take(uB).rej(new Error("stale B"));
  await tick();
  const midErr = boxes.s2.innerHTML;
  take(uA).res({dir: "A/", dirs: []
    , files: [{name: "new.py", size: 2}]});
  await q1; await q2; await q3;
  await tick();
  const finA = boxes.s2.innerHTML;
  console.log(JSON.stringify({
    sawB: sawB.indexOf('data-discevnav="s1|B"') >= 0,
    lateErrKept: finB === sawB,
    noErrText: finB.indexOf("목록 조회 실패") < 0,
    stalePainted: midStale.indexOf("stale.py") >= 0,
    staleErrPainted: midErr.indexOf("목록 조회 실패") >= 0,
    finA: finA.indexOf("new.py") >= 0,
    finNoStale: finA.indexOf("stale.py") < 0
  }));
})();
""")
        got = out[-1]
        self.assertTrue(got["sawB"], got)
        self.assertTrue(got["lateErrKept"], got)
        self.assertTrue(got["noErrText"], got)
        self.assertFalse(got["stalePainted"], got)
        self.assertFalse(got["staleErrPainted"], got)
        self.assertTrue(got["finA"], got)
        self.assertTrue(got["finNoStale"], got)

    def test_attempt_viewer_technical_record_uses_korean_result(self):
        out = run_driver("""
ATTEMPT = %s;
console.log(JSON.stringify({
  html: discAttemptHtml(ATTEMPT.attempt),
}));
""" % json.dumps(server_shaped_attempt()))
        html = out[-1]["html"]
        self.assertIn("결과: 완료", html)
        self.assertIn("기술 기록 보기", html)
        self.assertNotIn("결과 done", html)

    def test_review_flow_posts_owner_and_paints_report(self):
        out = run_driver("""
LIST = %s;
NODE = %s;
nodeSeq = [%s, %s];
REVIEW_RESP = {started: true};
globalThis.setTimeout = ((st) => (fn, ms) => st(fn, Math.min(ms, 15)))(setTimeout);
(async () => {
  discNodes["r:1"] = JSON.parse(JSON.stringify(NODE));
  discOpen.add("r:1");
  discSel["r"] = "r:1";
  await paintRepoCommunity({name: "r"});
  const p = discStartReview({name: "r"}, "r:1");
  await p;
  const html = tabEl.innerHTML;
  console.log(JSON.stringify({
    confirm: confirmCalls,
    by: postBodies["/api/discussions/review"].by,
    ev: postBodies["/api/discussions/review"].evidence,
    running: discPolling.size,
    report: html.includes("SUM "),
    revChip: html.includes("검토됨"),
  }));
})();
""" % (json.dumps(server_shaped_list()),
       json.dumps(server_shaped_node()),
       json.dumps(dict(server_shaped_node(),
                       review=dict(server_shaped_node()["review"],
                                   job={"status": "running"}))),
       json.dumps(server_shaped_node())))
        got = out[-1]
        self.assertEqual(got["confirm"], 1)
        self.assertEqual(got["by"], "owner")
        self.assertEqual(got["ev"], [])
        self.assertEqual(got["running"], 0)
        self.assertTrue(got["report"])
        self.assertTrue(got["revChip"])

    def test_review_poll_timeout_leaves_recoverable_state(self):
        running = dict(server_shaped_node(),
                       review=dict(server_shaped_node()["review"],
                                   job={"status": "running"}))
        out = run_driver("""
LIST = %s;
NODE = %s;
REVIEW_RESP = {started: true};
globalThis.setTimeout = ((st) => (fn, ms) => st(fn, Math.min(ms, 15)))(setTimeout);
(async () => {
  discNodes["r:1"] = JSON.parse(JSON.stringify(NODE));
  discOpen.add("r:1");
  discSel["r"] = "r:1";
  await paintRepoCommunity({name: "r"});
  await discStartReview({name: "r"}, "r:1");
  console.log(JSON.stringify({
    running: discPolling.size,
    stillRunning: tabEl.innerHTML.includes("실행 중"),
    toast: toasts.some(t => t.includes("아직 실행 중")),
  }));
})();
""" % (json.dumps(server_shaped_list()), json.dumps(running)))
        got = out[-1]
        self.assertEqual(got["running"], 0)
        self.assertTrue(got["stillRunning"])
        self.assertTrue(got["toast"])

    def test_review_button_disabled_when_model_unavailable(self):
        lst = server_shaped_list()
        lst["model"] = {"ok": False, "reason": "no login"}
        node = server_shaped_node()
        node["model"] = {"ok": False, "reason": "no login"}
        out = run_driver("""
LIST = %s;
NODE = %s;
(async () => {
  await paintRepoCommunity({name: "r"});
  await discToggleDetail({name: "r"}, "r:1", LIST.catalog);
  const html = tabEl.innerHTML;
  console.log(JSON.stringify({
    disabled: html.includes("disabled"),
    reason: html.includes("no login"),
  }));
})();
""" % (json.dumps(lst), json.dumps(node)))
        got = out[-1]
        self.assertTrue(got["disabled"])
        self.assertTrue(got["reason"])

    def test_evidence_picker_lists_bounded_files(self):
        out = run_driver("""
const evBox = mkBox();
document.querySelector = (sel) => sel.includes("discevbox") ? evBox : null;
SOURCES = {"repo": "r", "slug": "o/r", "dir": "",
  "dirs": ["sub"], "files": [{"name": "top.py", "size": 10},
  {"name": "big.bin", "size": 999999999, "too_large": true}],
  "truncated": false};
(async () => {
  await discEvidence({name: "r"}, "r:1", "");
  const html = evBox.innerHTML;
  console.log(JSON.stringify({
    dir: html.includes("sub"),
    file: html.includes("top.py"),
    tooLarge: html.includes("너무 큼"),
    disabledBig: /big\\.bin[^]*disabled/.test(html),
    cap: html.includes("최대 25건"),
  }));
})();
""")
        got = out[-1]
        self.assertTrue(got["dir"])
        self.assertTrue(got["file"])
        self.assertTrue(got["tooLarge"])
        self.assertTrue(got["disabledBig"])
        self.assertTrue(got["cap"])

WORKBENCH_FAKE_JS = r"""
// Binding-level workbench fake: buttons parsed from the rendered HTML get
// stable objects, so tests drive the exact onclick/onchange/onkeydown
// properties wireDiscRows/paintRepoCommunity wired (what the browser calls).
// It does NOT prove rendered scrolling or layout; that stays a browser gate.
const _wbFocusTrail = [];
const _wbScrollTrail = [];
function _wbParseButtons(html) {
  const key = "btns#" + html;
  if (tabEl._cache[key]) return tabEl._cache[key];
  const btns = [];
  const re = /<button\b[^>]*>/g;
  let m;
  while ((m = re.exec(html))) {
    const ds = {};
    const dre = /data-(discdetail|discrowbtn|discback|discattempt|discrev|discev|discevsel|discrow)="([^"]*)"/g;
    let dm;
    while ((dm = dre.exec(m[0]))) ds[dm[1]] = dm[2];
    if (!Object.keys(ds).length) continue;
    btns.push({dataset: ds, _h: {},
      set onclick(f) { this._h.click = f; },
      get onclick() { return this._h.click; },
      set onchange(f) { this._h.change = f; },
      get onchange() { return this._h.change; },
      focus() { _wbFocusTrail.push("btn:" + (ds.discrowbtn || ds.discback || "?")); },
      scrollIntoView() { _wbScrollTrail.push("btn:" + (ds.discrowbtn || ds.discback || "?")); }});
  }
  tabEl._cache[key] = btns;
  return btns;
}
function _wbParseDetails(html) {
  const key = "details#" + html;
  if (tabEl._cache[key]) return tabEl._cache[key];
  const out = [];
  const re = /<details\b[^>]*data-dk="([^"]+)"[^>]*>/g;
  let m;
  while ((m = re.exec(html))) {
    const tag = m[0];
    const o = {_open: /[\s]open(?=[\s>])/.test(tag), dataset: {dk: m[1]}};
    Object.defineProperty(o, "open", {
      get() { return this._open; },
      set(v) { this._open = !!v; },
      enumerable: true, configurable: true,
    });
    out.push(o);
  }
  tabEl._cache[key] = out;
  return out;
}
const _wbQSA0 = tabEl.querySelectorAll;
tabEl.querySelectorAll = (sel) => {
  if (sel === "details[data-dk]") {
    return _wbParseDetails(tabEl.innerHTML || "");
  }
  if (sel === "details[data-dk][open]") {
    return _wbParseDetails(tabEl.innerHTML || "").filter((d) => d.open);
  }
  const btns = (attr) => _wbParseButtons(tabEl.innerHTML || "").filter((b) => b.dataset[attr]);
  if (sel === "[data-discdetail]") return btns("discdetail");
  if (sel === "[data-discrowbtn]") return btns("discrowbtn");
  if (sel === "[data-discback]") return btns("discback");
  if (sel === "[data-discattempt]") return btns("discattempt");
  if (sel === "[data-discrev]") return btns("discrev");
  if (sel === "[data-discev]") return btns("discev");
  if (sel === "[data-discevsel]") return btns("discevsel");
  return _wbQSA0.call(tabEl, sel);
};
const _wbInsp = {scrollTop: 0, _h: {},
  set onkeydown(f) { this._h.key = f; },
  get onkeydown() { return this._h.key; }};
function _wbCardHtml(num) {
  const html = tabEl.innerHTML || "";
  const m = new RegExp('<article\\b[^>]*data-discdetailcard="' + num +
    '"[^>]*>([\\s\\S]*?)</article>').exec(html);
  return m ? m[1] : "";
}
function _wbCardEl(num) {
  return {dataset: {discdetailcard: num},
    querySelectorAll(sel) {
      if (sel === "details[data-dk]") return _wbParseDetails(_wbCardHtml(num));
      return [];
    },
    querySelector(sel) {
      const dm = /^(?:details)?\[data-dk="(.+)"\]$/.exec(sel);
      if (dm) {
        return _wbParseDetails(_wbCardHtml(num))
          .find((d) => d.dataset.dk === dm[1]) || null;
      }
      return null;
    }};
}
const _wbQS0 = tabEl.querySelector;
tabEl.querySelector = (sel) => {
  if (sel === "[data-discdetailcard]") {
    const m = /data-discdetailcard="([^"]+)"/.exec(tabEl.innerHTML || "");
    return m ? _wbCardEl(m[1]) : null;
  }
  const m = /^\[data-discdetailcard="(.+)"\]$/.exec(sel);
  if (m) {
    return (tabEl.innerHTML || "").includes('data-discdetailcard="' + m[1] + '"')
      ? {dataset: {discdetailcard: m[1]}} : null;
  }
  if (sel === ".winspect") {
    return (tabEl.innerHTML || "").includes('id="disc-inspector"') ? _wbInsp : null;
  }
  {
    const dm = /^\[data-dk="(.+)"\]$/.exec(sel) ||
      /^details\[data-dk="(.+)"\]$/.exec(sel);
    if (dm) {
      const hit = _wbParseDetails(tabEl.innerHTML || "")
        .find((d) => d.dataset.dk === dm[1]);
      return hit || null;
    }
  }
  return _wbQS0.call(tabEl, sel);
};
const _wbDQS0 = document.querySelector;
const _wbLive = {};
function _wbFocusEl(name) {
  return {focus() { _wbFocusTrail.push(name); },
    scrollIntoView() { _wbScrollTrail.push(name);
      /* Rendered-browser side effect (measured 1010->26 on the real
         inspector): scrolling the heading into view resets the enclosing
         inspector reading position. The regression test below fails
         without the product keptPos guard. */
      if (typeof _wbInsp !== "undefined") _wbInsp.scrollTop = 26; }};
}
document.querySelector = (sel) => {
  let m = /^\[data-discfocus="(.+)"\]$/.exec(sel);
  if (m) return _wbFocusEl("focus:" + m[1]);
  m = /^\[data-discattemptbox="(.+)"\]$/.exec(sel);
  if (m) {
    if (!_wbLive[m[1]]) _wbLive[m[1]] = {innerHTML: ""};
    return _wbLive[m[1]];
  }
  m = /^\[data-discmsg="(.+)"\]$/.exec(sel);
  if (m) return {set textContent(v) {}, get textContent() { return ""; }};
  m = /^\[data-discrowbtn="(.+)"\]$/.exec(sel);
  if (m) {
    const hit = _wbParseButtons(tabEl.innerHTML || "")
      .find((b) => b.dataset.discrowbtn === m[1]);
    return hit ? {dataset: hit.dataset,
      focus() { _wbFocusTrail.push("row:" + m[1]); },
      scrollIntoView() { _wbScrollTrail.push("row:" + m[1]); }} : null;
  }
  return _wbDQS0.call(document, sel);
};
function _wbClick(btn) { return btn.onclick(); }
const _wbSleep = (ms) => new Promise((res) => setTimeout(res, ms));
// Number-aware node stub: the product asks per number, so the fake answers
// per number from the driver-supplied NODES map (falls back to base jget).
function _wbNumberAware(NODES) {
  const base = jget;
  jget = (u) => {
    const m = /^\/api\/discussions\/node\?repo=[^&]*&number=(\d+)/.exec(u);
    if (m && NODES[m[1]]) {
      nodeCalls++;
      const pay = NODES[m[1]];
      return new Promise((res) => setTimeout(
        () => res(JSON.parse(JSON.stringify(pay))), 5));
    }
    return base(u);
  };
}
"""


def review_state_js():
    """Real capture/restore helpers, brace-matched (not the noop stubs)."""
    src = Path(__file__).resolve().parent.parent / "src" / "openmainty" / "dashboard.html"
    html = src.read_text(encoding="utf-8")
    out = []
    for sig in ("function captureReviewState(box) {",
                "function revQueueTop(box) {",
                "function restoreRevQueueTop(box, top) {",
                "function restoreReviewState(box, st) {",
                "async function restoreReviewStateAsync(box, st) {"):
        i = html.index(sig)
        j = html.index("{", i)
        depth = 0
        k = j
        instr = None
        while True:
            ch = html[k]
            if instr:
                if ch == instr:
                    instr = None
            elif ch in "\"'`":
                instr = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        out.append(html[i:k + 1])
    return "\n".join(out)


def hash_js():
    """Real parseHash/syncHash, brace-matched (not stubs)."""
    src = Path(__file__).resolve().parent.parent / "src" / "openmainty" / "dashboard.html"
    html = src.read_text(encoding="utf-8")
    out = []
    for sig in ("function parseHash() {", "function syncHash() {"):
        i = html.index(sig)
        j = html.index("{", i)
        depth = 0
        k = j
        instr = None
        while True:
            ch = html[k]
            if instr:
                if ch == instr:
                    instr = None
            elif ch in "\"'`":
                instr = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        out.append(html[i:k + 1])
    return "\n".join(out)


POLL_FAKE_JS = r"""
const chgLoads = new Map();
function scrollerOf(box) { return null; }
let boxGen = 0;
const liveBoxes = {};
let openReg = {};
let restoredKeys = [];
let attemptCalls = 0;
document.activeElement = null;
document.body = document.body || {};
document.scrollingElement = null;
document.documentElement = null;
function makeTracker(k) {
  return {querySelector: () => null,
    set open(v) { if (v) { openReg[k] = true; restoredKeys.push(k); } else delete openReg[k]; },
    get open() { return !!openReg[k]; }};
}
document.querySelector = (sel) => {
  let m = /^\[data-discattemptbox="(.+)"\]$/.exec(sel);
  if (m) {
    const k = "att:" + boxGen + ":" + m[1];
    if (!liveBoxes[k]) liveBoxes[k] = {innerHTML: ""};
    return liveBoxes[k];
  }
  m = /^\[data-discevbox="(.+)"\]$/.exec(sel);
  if (m) {
    const k = "ev:" + boxGen + ":" + m[1];
    if (!liveBoxes[k]) liveBoxes[k] = {innerHTML: "", querySelectorAll: () => [], querySelector: () => null};
    return liveBoxes[k];
  }
  m = /^\[data-discmsg="(.+)"\]$/.exec(sel);
  if (m) return {set textContent(v) {}, get textContent() { return ""; }};
  m = /^\[data-discevsel="(.+)"\]$/.exec(sel);
  if (m) return {set textContent(v) {}, get textContent() { return ""; }};
  m = /^details\[data-dk="(.+)"\]$/.exec(sel);
  if (m) return makeTracker(m[1]);
  return null;
};
const dkRe = /<details[^>]*data-dk="([^"]+)"[^>]*>/g;
function combinedHtml() {
  let h = tabEl._html || "";
  for (const k of Object.keys(liveBoxes)) {
    if (k.indexOf(":" + boxGen + ":") < 0) continue;
    h += liveBoxes[k].innerHTML || "";
  }
  return h;
}
tabEl.querySelector = (sel) => {
  const m = /^details\[data-dk="(.+)"\]$/.exec(sel);
  if (m) return makeTracker(m[1]);
  return null;
};
tabEl.querySelectorAll = (sel) => {
  if (sel === "[data-dkf]") return findSelects(tabEl._html);
  if (sel === "details[data-dk][open]") {
    const keys = new Set(); let m; dkRe.lastIndex = 0;
    const h = combinedHtml();
    while ((m = dkRe.exec(h))) keys.add(m[1]);
    return [...keys].filter(k => openReg[k]).map(k => ({dataset: {dk: k}}));
  }
  if (sel === "[data-discevbox]") {
    return Object.keys(discEvOpen).filter(k => discEvCache[k + "|" + discEvOpen[k]]).map(k => ({
      dataset: {discevbox: k},
      get innerHTML() { return "x"; },
      querySelectorAll: () => [],
    }));
  }
  return [];
};
// Repaint rebuilds the DOM: every tabEl rewrite orphans looked-up boxes.
const _d = Object.getOwnPropertyDescriptor(tabEl, "innerHTML");
Object.defineProperty(tabEl, "innerHTML", {
  set(v) { boxGen++; _d.set.call(this, v); },
  get() { return _d.get.call(this); },
  configurable: true,
});
const _jgPoll = jget;
jget = (u) => {
  if (u.indexOf("/api/discussions/attempt") === 0) {
    attemptCalls++;
    if (u.indexOf("review-1-rA") >= 0 && gateA) {
      return new Promise((res) => { gateA.push(() => res(JSON.parse(JSON.stringify(ATTEMPT_A)))); });
    }
    const pay = u.indexOf("review-1-rB") >= 0 ? ATTEMPT_B
      : (u.indexOf("review-1-rE") >= 0 ? ATTEMPT_E : ATTEMPT_A);
    return new Promise((res) => setTimeout(() => res(JSON.parse(JSON.stringify(pay))), 1));
  }
  return _jgPoll(u);
};
let gateA = null;
"""


def attempt_variant(rid, artifact, excerpt, outcome="done", error=None):
    a = server_shaped_attempt()
    canon = json.dumps({"title": "T " + rid, "body": "body " + rid,
                        "category": "Ideas", "author": "a",
                        "comments": [{"author": "c", "body": "cc"}]},
                       ensure_ascii=False)
    att = dict(a["attempt"], rid=rid, outcome=outcome, error=error,
               input_snapshot=dict(
                   a["attempt"]["input_snapshot"],
                   discussion=canon,
                   sources=[{"path": "a.py",
                             "sha256": "abc123def456",
                             "bytes_total": 20, "bytes_sent": 20,
                             "truncated": False, "excerpt": excerpt}],
                   sources_requested=["a.py"],
                   sources_excluded=[]),
               report={"summary": "SUM " + rid, "findings": ["F1"],
                       "source_files": ["a.py"]})
    return {"repo": "r", "slug": "o/r", "number": 1,
            "artifact": artifact, "attempt": att}


def stale_node(new_artifact):
    node = server_shaped_node()
    rev = dict(node["review"], stale=True, status="stale",
               artifact=new_artifact,
               history=node["review"]["history"] + [
                   {"at": "2026-10-25T03:00:00+00:00",
                    "event": "job-done",
                    "artifact": new_artifact}])
    return dict(node, review=rev)


def stale_list(new_artifact):
    lst = server_shaped_list()
    items = [dict(lst["items"][0],
                  review=dict(lst["items"][0]["review"], stale=True,
                              status="stale"))]
    return dict(lst, items=items,
                status=dict(lst["status"],
                            checked_at="2026-10-25T03:00:00+00:00"))


class PollPreservationTests(unittest.TestCase):
    def test_open_attempt_survives_status_repaint_with_history_note(self):
        out = run_driver("""
LIST = %s;
NODE = %s;
ATTEMPT_A = %s;
ATTEMPT_B = %s;
(async () => {
  await paintRepoCommunity({name: "r"});
  await discToggleDetail({name: "r"}, "r:1", LIST.catalog);
  await discLoadAttempt({name: "r"}, "r:1|review-1-rA.json");
  openReg["disc-att-rA-disc"] = true;
  openReg["disc-att-rA-file-0"] = true;
  LIST = %s;
  NODE = %s;
  delete discCache["r"];
  await paintRepoCommunity({name: "r"});
  const html = tabEl.innerHTML;
  console.log(JSON.stringify({
    viewer: html.includes("print(1)"),
    rid: html.includes("rA"),
    fileKey: html.includes('data-dk="disc-att-rA-file-0"'),
    discKey: html.includes('data-dk="disc-att-rA-disc"'),
    kept: html.includes("보관된 시도") && html.includes("review-1-rB.json"),
    stale: html.includes("재검토 필요"),
    restored: restoredKeys,
    attemptCalls: attemptCalls,
    posts: Object.keys(postBodies),
  }));
})();
""" % (json.dumps(server_shaped_list()),
       json.dumps(server_shaped_node()),
       json.dumps(attempt_variant("rA", "review-1-rA.json",
                                  "print(1)\n")),
       json.dumps(attempt_variant("rB", "review-1-rB.json",
                                  "print(2)\n")),
       json.dumps(stale_list("review-1-rB.json")),
       json.dumps(stale_node("review-1-rB.json"))),
            prelude_extra=review_state_js() + POLL_FAKE_JS)
        got = out[-1]
        # The same opened viewer is re-rendered inline after the repaint.
        self.assertTrue(got["viewer"], got)
        self.assertTrue(got["rid"], got)
        # Nested disclosure keys survive so the generic restore can reopen.
        self.assertTrue(got["fileKey"], got)
        self.assertTrue(got["discKey"], got)
        self.assertIn("disc-att-rA-file-0", got["restored"])
        self.assertIn("disc-att-rA-disc", got["restored"])
        # Newer report is distinguished, not swapped in.
        self.assertTrue(got["kept"], got)
        self.assertTrue(got["stale"], got)
        # No refetch of the immutable attempt, no model call on poll.
        self.assertEqual(got["attemptCalls"], 1)
        self.assertNotIn("/api/discussions/review", got["posts"])

    def test_leave_and_return_keeps_viewer_without_refetch(self):
        out = run_driver("""
LIST = %s;
NODE = %s;
ATTEMPT_A = %s;
ATTEMPT_B = %s;
(async () => {
  await paintRepoCommunity({name: "r"});
  await discToggleDetail({name: "r"}, "r:1", LIST.catalog);
  await discLoadAttempt({name: "r"}, "r:1|review-1-rA.json");
  await discToggleDetail({name: "r"}, "r:1", LIST.catalog);
  await paintRepoCommunity({name: "r"});
  const closedHtml = tabEl.innerHTML;
  await discToggleDetail({name: "r"}, "r:1", LIST.catalog);
  const html = tabEl.innerHTML;
  console.log(JSON.stringify({
    closedGone: !closedHtml.includes("print(1)"),
    viewerBack: html.includes("print(1)"),
    attemptCalls: attemptCalls,
  }));
})();
""" % (json.dumps(server_shaped_list()),
       json.dumps(server_shaped_node()),
       json.dumps(attempt_variant("rA", "review-1-rA.json",
                                  "print(1)\n")),
       json.dumps(attempt_variant("rB", "review-1-rB.json",
                                  "print(2)\n"))),
            prelude_extra=review_state_js() + POLL_FAKE_JS)
        got = out[-1]
        self.assertTrue(got["closedGone"], got)
        self.assertTrue(got["viewerBack"], got)
        self.assertEqual(got["attemptCalls"], 1)

    def test_slow_old_attempt_does_not_clobber_new_selection(self):
        out = run_driver("""
LIST = %s;
NODE = %s;
ATTEMPT_A = %s;
ATTEMPT_B = %s;
gateA = [];
(async () => {
  await paintRepoCommunity({name: "r"});
  await discToggleDetail({name: "r"}, "r:1", LIST.catalog);
  const pA = discLoadAttempt({name: "r"}, "r:1|review-1-rA.json");
  await discLoadAttempt({name: "r"}, "r:1|review-1-rB.json");
  gateA.forEach((fn) => fn());
  gateA = null;
  await pA;
  const box = document.querySelector('[data-discattemptbox="r:1"]');
  console.log(JSON.stringify({
    showsB: (box.innerHTML || "").includes("print(2)"),
    hidesA: !(box.innerHTML || "").includes("print(1)"),
    selected: discAttOpen["r:1"],
    cachedKeys: Object.keys(discAttCache),
  }));
})();
""" % (json.dumps(server_shaped_list()),
       json.dumps(server_shaped_node()),
       json.dumps(attempt_variant("rA", "review-1-rA.json",
                                  "print(1)\n")),
       json.dumps(attempt_variant("rB", "review-1-rB.json",
                                  "print(2)\n"))),
            prelude_extra=review_state_js() + POLL_FAKE_JS)
        got = out[-1]
        self.assertTrue(got["showsB"], got)
        self.assertTrue(got["hidesA"], got)
        self.assertEqual(got["selected"], "review-1-rB.json")
        self.assertNotIn("r:1|review-1-rA.json", got["cachedKeys"])

    def test_evidence_picker_and_filter_survive_repaint(self):
        out = run_driver("""
LIST = %s;
NODE = %s;
SOURCES = {"repo": "r", "slug": "o/r", "dir": "",
  "dirs": ["sub"], "files": [{"name": "a.py", "size": 39}],
  "truncated": false};
(async () => {
  await paintRepoCommunity({name: "r"});
  await discToggleDetail({name: "r"}, "r:1", LIST.catalog);
  await discEvidence({name: "r"}, "r:1", "");
  discEvSel["r:1"] = ["a.py"];
  discFPlatform = "android";
  delete discCache["r"];
  await paintRepoCommunity({name: "r"});
  const html = tabEl.innerHTML;
  console.log(JSON.stringify({
    picker: html.includes("근거 파일 고르기") && html.includes("a.py"),
    checked: /a\\.py[^]*checked|checked[^]*a\\.py/.test(html),
    row1: html.includes("/discussions/1"),
    row858gone: !html.includes("/discussions/858"),
    filterKept: html.includes('value="android" selected'),
  }));
})();
""" % (json.dumps(server_shaped_list()),
       json.dumps(server_shaped_node())),
            prelude_extra=review_state_js() + POLL_FAKE_JS)
        got = out[-1]
        self.assertTrue(got["picker"], got)
        self.assertTrue(got["checked"], got)
        self.assertTrue(got["row1"], got)
        self.assertTrue(got["row858gone"], got)
        self.assertTrue(got["filterKept"], got)

    def test_failed_first_attempt_loads_and_survives_repaint(self):
        node = server_shaped_node()
        err = dict(node["review"], artifact=None, status="error",
                   job={"status": "error", "rid": "rE",
                        "error": "모델 할당량 소진; 한도 회복 후 수동으로 다시 요청"},
                   history=[{"at": "2026-10-25T01:00:00+00:00",
                             "event": "job-error",
                             "artifact": "review-1-rE.json",
                             "error": "quota"}])
        lst = server_shaped_list()
        lst2 = dict(lst, status=dict(lst["status"],
                                    checked_at="2026-10-25T04:00:00+00:00"))
        out = run_driver("""
LIST = %s;
NODE = %s;
ATTEMPT_E = %s;
(async () => {
  await paintRepoCommunity({name: "r"});
  await discToggleDetail({name: "r"}, "r:1", LIST.catalog);
  const before = tabEl.innerHTML;
  await discLoadAttempt({name: "r"}, "r:1|review-1-rE.json");
  LIST = %s;
  delete discCache["r"];
  await paintRepoCommunity({name: "r"});
  const html = tabEl.innerHTML;
  console.log(JSON.stringify({
    failBtn: before.includes("실패 시도 근거 보기"),
    noAcceptBtn: !before.includes("검토에 쓴 원문"),
    boxTarget: before.includes('data-discattemptbox="r:1"'),
    errViewer: html.includes("rE"),
    noReportNote: html.includes("승인된 보고서 없음"),
    stillError: html.includes("작업: 실패") && html.includes("오류"),
    attemptCalls: attemptCalls,
    posts: Object.keys(postBodies),
  }));
})();
""" % (json.dumps(lst), json.dumps(dict(node, review=err)),
       json.dumps(attempt_variant("rE", "review-1-rE.json", "print(9)\n",
                                  outcome="error", error="quota")),
       json.dumps(lst2)),
            prelude_extra=review_state_js() + POLL_FAKE_JS)
        got = out[-1]
        self.assertTrue(got["failBtn"], got)
        self.assertTrue(got["boxTarget"], got)
        self.assertTrue(got["errViewer"], got)
        self.assertTrue(got["noReportNote"], got)
        self.assertTrue(got["stillError"], got)
        self.assertTrue(got["noAcceptBtn"], got)
        self.assertEqual(got["attemptCalls"], 1)
        self.assertNotIn("/api/discussions/review", got["posts"])

    def test_invalid_report_primary_copy_keeps_diagnostic_secondary(self):
        node = server_shaped_node()
        err = dict(node["review"], report=None, artifact=None,
                   job={"status": "error", "rid": "r1",
                        "error": "model output failed validation: "
                                 "report labels.status is not a list",
                        "error_kind": "invalid-report"})
        out = run_driver("""
LIST = %s;
NODE = %s;
(async () => {
  await paintRepoCommunity({name: "r"});
  await discToggleDetail({name: "r"}, "r:1", LIST.catalog);
  console.log(JSON.stringify({html: tabEl.innerHTML}));
})();
""" % (json.dumps(server_shaped_list()),
       json.dumps(dict(node, review=err))))
        html = out[-1]["html"]
        # Primary alert: short Korean copy, not raw validator prose.
        self.assertIn("출력 형식 검증 실패 · 승인된 보고서 없음", html)
        alert = html.split('role="alert"')[1].split("</div>")[0]
        self.assertNotIn("is not a list", alert)
        # Exact diagnostic stays inspectable in the execution record.
        self.assertIn("실행 기록 보기", html)
        self.assertIn("report labels.status is not a list", html)

    def test_invalid_report_keeps_previous_accepted_report(self):
        node = server_shaped_node()
        err = dict(node["review"],
                   job={"status": "error", "rid": "r2",
                        "error": "model output failed validation: x",
                        "error_kind": "invalid-report"})
        out = run_driver("""
LIST = %s;
NODE = %s;
(async () => {
  await paintRepoCommunity({name: "r"});
  await discToggleDetail({name: "r"}, "r:1", LIST.catalog);
  console.log(JSON.stringify({html: tabEl.innerHTML}));
})();
""" % (json.dumps(server_shaped_list()),
       json.dumps(dict(node, review=err))))
        html = out[-1]["html"]
        self.assertIn("출력 형식 검증 실패 · 이전 보고서는 유지됨", html)
        # The older accepted report stays readable and immutable.
        self.assertIn("SUM", html)
        self.assertNotIn("승인된 보고서 없음", html)

    def test_failed_attempt_copy_distinguishes_never_attempted(self):
        node = server_shaped_node()
        failed = dict(node["review"], artifact=None, report=None,
                      job={"status": "error", "rid": "rE",
                           "error": "model output failed validation: x",
                           "error_kind": "invalid-report"},
                      history=[{"at": "2026-10-25T01:00:00+00:00",
                                "event": "job-error",
                                "artifact": "review-1-rE.json",
                                "error": "invalid-report"}])
        fresh = dict(node["review"], status="unreviewed",
                     artifact=None, report=None,
                     job={"status": "idle"}, history=[])
        base_list = server_shaped_list()
        fresh_list = dict(
            base_list, status=dict(base_list["status"]),
            items=[dict(base_list["items"][0],
                        review={"status": "unreviewed", "labels": []}),
                   base_list["items"][1]])
        out = run_driver("""
LIST = %s;
NODE = %s;
LIST_FRESH = %s;
NODE_FRESH = %s;
(async () => {
  await paintRepoCommunity({name: "r"});
  await discToggleDetail({name: "r"}, "r:1", LIST.catalog);
  const tried = tabEl.innerHTML;
  await discToggleDetail({name: "r"}, "r:1", LIST.catalog);
  LIST = LIST_FRESH;
  NODE = NODE_FRESH;
  delete discCache["r"];
  delete discNodes["r:1"];
  await discToggleDetail({name: "r"}, "r:1", LIST.catalog);
  const fresh = tabEl.innerHTML;
  console.log(JSON.stringify({tried, fresh}));
})();
""" % (json.dumps(server_shaped_list()),
       json.dumps(dict(node, review=failed)),
       json.dumps(fresh_list),
       json.dumps(dict(node, review=fresh))))
        self.assertIn("승인된 보고서 없음", out[-1]["tried"])
        self.assertNotIn("아직 앱 검토 없음", out[-1]["tried"])
        self.assertIn("아직 앱 검토 없음", out[-1]["fresh"])
        self.assertNotIn("승인된 보고서 없음", out[-1]["fresh"])

    def test_opened_historical_attempt_keeps_human_notice(self):
        out = run_driver("""
LIST = %s;
NODE = %s;
ATTEMPT_A = %s;
(async () => {
  await paintRepoCommunity({name: "r"});
  await discToggleDetail({name: "r"}, "r:1", LIST.catalog);
  discAttOpen["r:1"] = "review-1-rA.json";
  discAttCache["r:1|review-1-rA.json"] = ATTEMPT_A.attempt;
  delete discCache["r"];
  await paintRepoCommunity({name: "r"});
  const html = tabEl.innerHTML;
  const notice = (html.match(/보관된 시도를 보고[^<]*/) || [""])[0];
  console.log(JSON.stringify({
    viewer: html.includes("print(1)"),
    notice,
    hasFileName: notice.includes(".json"),
    hasTime: notice.includes("시도 시각"),
  }));
})();
""" % (json.dumps(server_shaped_list()),
       json.dumps(stale_node("review-1-rB.json")),
       json.dumps(attempt_variant("rA", "review-1-rA.json",
                                  "print(1)\n"))),
            prelude_extra=review_state_js() + POLL_FAKE_JS)
        got = out[-1]
        self.assertTrue(got["viewer"], got)
        self.assertIn("보관된 시도", got["notice"])
        self.assertFalse(got["hasFileName"], got)
        self.assertTrue(got["hasTime"], got)

    def test_null_report_viewer_marks_citation_unverified(self):
        failed = attempt_variant("rE", "review-1-rE.json", "print(9)\n",
                                 outcome="error", error="invalid-report")
        failed["attempt"] = dict(failed["attempt"], report=None)
        out = run_driver("""
ATTEMPT = %s;
VALID = %s;
console.log(JSON.stringify({
  failed: discAttemptHtml(ATTEMPT.attempt),
  valid: discAttemptHtml(VALID.attempt),
}));
""" % (json.dumps(failed),
       json.dumps(attempt_variant("rA", "review-1-rA.json",
                                  "print(1)\n"))))
        bad = out[-1]["failed"]
        # No accepted report: no zero/inverse citation claims.
        self.assertIn("인용 미확인", bad)
        self.assertIn("미확인", bad)
        self.assertNotIn("인용 0건", bad)
        self.assertNotIn("미인용", bad)
        # Requested/sent/bytes facts stay exact.
        self.assertIn("요청 1건", bad)
        self.assertIn("전달 1건", bad)
        self.assertIn("print(9)", bad)
        # A valid report keeps its real cited counts.
        good = out[-1]["valid"]
        self.assertIn("인용 1건", good)
        self.assertIn("인용됨", good)

    def test_safe_urls_only(self):
        out = run_driver("""
console.log(JSON.stringify({
  good: safeGhUrl("https://github.com/o/r/discussions/858"),
  issue: safeGhUrl("https://github.com/o/r/issues/1"),
  js: safeGhUrl("javascript:alert(1)"),
  other: safeGhUrl("https://evil.example/x"),
  quote: safeGhUrl('https://github.com/o/r/discussions/1"onmouseover="x'),
}));
""")
        got = out[-1]
        self.assertTrue(got["good"].endswith("/discussions/858"))
        self.assertTrue(got["issue"].endswith("/issues/1"))
        self.assertIsNone(got["js"])
        self.assertIsNone(got["other"])
        self.assertIsNone(got["quote"])


def wb_node(number, title, status, job_status="idle", job_error=None):
    node = server_shaped_node()
    node["node"] = dict(node["node"], number=number, title=title,
                        url="https://github.com/o/r/discussions/%d" % number)
    node["review"] = dict(node["review"], status=status, stale=False,
                          job={"status": job_status,
                               "error": job_error,
                               "rid": "r%d" % number})
    return node


def wb_list(items):
    base = server_shaped_list()
    base["items"] = items
    return base


def wb_item(number, title, status, category="Ideas", comments=1):
    return {"id": "D%d" % number, "type": "discussion", "number": number,
            "title": title, "category": category, "comments": comments,
            "updated_at": "2026-10-24T00:00:00+00:00",
            "url": "https://github.com/o/r/discussions/%d" % number,
            "review": {"status": status, "stale": False,
                       "partial": False, "detail_complete": True,
                       "labels": [], "label_facets": {},
                       "source_unavailable": False,
                       "job_status": "idle", "job_error": None}}


class WorkbenchFlowTests(unittest.TestCase):
    """Queue/inspector user flows through real event bindings.

    Drivers press the exact onclick/onchange/onkeydown properties the
    loader wired (what the browser calls) and run the real
    paint/reconcile/syncHash code; they never reset discFpVal or set
    discSel by hand. The reload case stages a copied boot/hash sequence
    plus the real parseHash/syncHash: a contract/unit slice, not the
    whole real initializer (repeated-reload browser evidence is the
    real initialization proof). Binding-level proof only: rendered
    scrolling and layout stay a browser gate.
    """

    def test_reload_keeps_disc_through_real_init_and_sync(self):
        node1 = wb_node(1, "Opus support", "reviewed")
        node858 = wb_node(858, "Plain question", "unreviewed")
        out = run_driver("""
var location = {_h: "#/repo/r?tab=community&disc=858",
  get hash() { return this._h; }};
var history = {replaceState: function(a, b, h) { location._h = h; }};
LIST = %s;
NODES = %s;
_wbNumberAware(NODES);
function stageHash() {
  // Verbatim from the boot/hashchange init lines: parse, then stage.
  if (location.hash) {
    const v = parseHash();
    if (v.kind === "repo") { repoTab = v.tab || "overview"; if (v.batch) pendingBatchScroll = v.batch; if (v.rollup) pendingRollupScroll = v.rollup; if (v.disc) { pendingDiscSel = {repo: v.id, num: v.disc}; discNavFocus = true; } view = v; }
    else view = v;
  } else view = {kind: "glance"};
}
async function syncThenPaint() {
  stageHash();
  syncHash();  /* render() calls syncHash before paint applies pending */
  const h1 = location.hash;
  await paintRepoCommunity({name: "r"});
  const sel = discSelectedNum("r");
  const html = tabEl.innerHTML;
  syncHash();
  return {h1: h1, h2: location.hash, sel: sel, html: html};
}
(async () => {
  const first = await syncThenPaint();
  // Same-document move while 858 is cached: incoming pending must win
  // over the cached selection before paint applies it.
  location._h = "#/repo/r?tab=community&disc=1";
  stageHash();
  syncHash();
  const navHash = location.hash;
  await paintRepoCommunity({name: "r"});
  const navSel = discSelectedNum("r");
  const navHtml = tabEl.innerHTML;
  syncHash();
  const navHash2 = location.hash;
  // Fresh full load with the preserved URL.
  discSel = {}; pendingDiscSel = null; discNavFocus = false;
  discOpen = new Set(); discNodes = {}; discCache = {};
  discFetchedAt = {}; discFpVal = {}; discScroll = {};
  location._h = navHash2;
  const second = await syncThenPaint();
  console.log(JSON.stringify({first: first, navHash: navHash,
    navSel: navSel, navOpus: navHtml.includes("Opus support"),
    navHash2: navHash2, second: second}));
})();
""" % (json.dumps(wb_list(
            [wb_item(1, "Opus support", "reviewed"),
             wb_item(858, "Plain question", "unreviewed")])),
       json.dumps({"1": node1, "858": node858})),
            prelude_extra=WORKBENCH_FAKE_JS + hash_js())
        got = out[-1]
        first = got["first"]
        # Before the fix the first syncHash already dropped disc.
        self.assertIn("disc=858", first["h1"], got)
        self.assertEqual(first["sel"], "858", got)
        self.assertIn("Plain question", first["html"], got)
        self.assertIn("disc=858", first["h2"], got)
        # Cached 858 must not override the incoming 1 target.
        self.assertIn("disc=1", got["navHash"], got)
        self.assertNotIn("disc=858", got["navHash"], got)
        self.assertEqual(got["navSel"], "1", got)
        self.assertTrue(got["navOpus"], got)
        self.assertIn("disc=1", got["navHash2"], got)
        # The preserved URL restores the same inspector on next load.
        second = got["second"]
        self.assertIn("disc=1", second["h1"], got)
        self.assertEqual(second["sel"], "1", got)
        self.assertIn("Opus support", second["html"], got)
        self.assertIn("disc=1", second["h2"], got)

    def test_selection_roundtrip_through_click_bindings(self):
        node858 = wb_node(858, "Opus support for Android", "reviewed")
        node859 = wb_node(859, "Plain question", "error",
                          job_status="error",
                          job_error="preview fixture: model disabled")
        out = run_driver("""
LIST = %s;
NODES = %s;
_wbNumberAware(NODES);
function rowBtn(key) {
  return tabEl.querySelectorAll("[data-discdetail]")
    .find((b) => b.dataset.discdetail === key);
}
(async () => {
  await paintRepoCommunity({name: "r"});
  const head = tabEl.innerHTML;
  await _wbClick(rowBtn("r:858"));
  const html858 = tabEl.innerHTML;
  await _wbClick(rowBtn("r:859"));
  const html859 = tabEl.innerHTML;
  await _wbClick(rowBtn("r:858"));
  const htmlBack = tabEl.innerHTML;
  console.log(JSON.stringify({
    headErr: head.includes("오류 1건"),
    titleFirst: html859.indexOf("<b>Plain question") <
      html859.indexOf("검토: 오류"),
    card858: html858.includes('id="disc-card-858"') &&
      !html858.includes('id="disc-card-859"'),
    head858: html858.includes('data-discfocus="858"') &&
      html858.includes("Opus support for Android"),
    card859: html859.includes('id="disc-card-859"') &&
      !html859.includes('id="disc-card-858"'),
    head859: html859.includes('data-discfocus="859"') &&
      html859.includes("검토 실패"),
    cardBack: htmlBack.includes('id="disc-card-858"') &&
      !htmlBack.includes('id="disc-card-859"') &&
      htmlBack.includes('data-discfocus="858"'),
    selBack: discSelectedNum("r") === "858",
    focusTrail: _wbFocusTrail.slice(-3),
  }));
})();
""" % (json.dumps(wb_list(
            [wb_item(858, "Opus support for Android (preview fixture)",
                     "reviewed"),
             wb_item(859, "Plain question (preview fixture)", "error",
                     category="Q&A", comments=0)])),
       json.dumps({"858": node858, "859": node859})),
            prelude_extra=WORKBENCH_FAKE_JS)
        got = out[-1]
        self.assertTrue(got["headErr"], got)
        self.assertTrue(got["titleFirst"], got)
        self.assertTrue(got["card858"], got)
        self.assertTrue(got["head858"], got)
        self.assertTrue(got["card859"], got)
        self.assertTrue(got["head859"], got)
        self.assertTrue(got["cardBack"], got)
        self.assertTrue(got["selBack"], got)
        self.assertIn("focus:859", got["focusTrail"], got)

    def test_filter_to_zero_keeps_detail_with_note(self):
        node858 = wb_node(858, "Opus support for Android", "reviewed")
        out = run_driver("""
LIST = %s;
NODE = %s;
function rowBtn(key) {
  return tabEl.querySelectorAll("[data-discdetail]")
    .find((b) => b.dataset.discdetail === key);
}
(async () => {
  await paintRepoCommunity({name: "r"});
  await _wbClick(rowBtn("r:858"));
  const sels = tabEl.querySelectorAll("[data-dkf]");
  const st = sels.find((s) => s.dataset.dkf === "status");
  st.value = "unreviewed";
  st.onchange();
  await _wbSleep(120);
  const htmlEmpty = tabEl.innerHTML;
  const st2 = tabEl.querySelectorAll("[data-dkf]")
    .find((s) => s.dataset.dkf === "status");
  st2.value = "";
  st2.onchange();
  await _wbSleep(120);
  const htmlBack = tabEl.innerHTML;
  console.log(JSON.stringify({
    emptyRows: (htmlEmpty.match(/data-discrow="/g) || []).length,
    keptTitle: htmlEmpty.includes("Opus support for Android"),
    hiddenNote: htmlEmpty.includes("필터에 가려져 있습니다"),
    rowsBack: (htmlBack.match(/data-discrow="/g) || []).length,
    selBack: discSelectedNum("r") === "858" &&
      htmlBack.includes("Opus support for Android"),
  }));
})();
""" % (json.dumps(wb_list(
            [wb_item(1, "Other", "reviewed"),
             wb_item(858, "Opus support for Android", "reviewed")])),
       json.dumps(node858)),
            prelude_extra=WORKBENCH_FAKE_JS)
        got = out[-1]
        self.assertEqual(got["emptyRows"], 0, got)
        self.assertTrue(got["keptTitle"], got)
        self.assertTrue(got["hiddenNote"], got)
        self.assertEqual(got["rowsBack"], 2, got)
        self.assertTrue(got["selBack"], got)

    def test_viewer_and_scroll_survive_switch_and_quiet_poll(self):
        node1 = wb_node(1, "Opus support", "reviewed")
        node858 = wb_node(858, "Plain question", "unreviewed")
        out = run_driver("""
LIST = %s;
NODES = %s;
ATTEMPT = %s;
_wbNumberAware(NODES);
function rowBtn(key) {
  return tabEl.querySelectorAll("[data-discdetail]")
    .find((b) => b.dataset.discdetail === key);
}
(async () => {
  await paintRepoCommunity({name: "r"});
  await _wbClick(rowBtn("r:1"));
  const att = tabEl.querySelectorAll("[data-discattempt]")[0];
  await _wbClick(att);
  await _wbSleep(120);
  await paintRepoCommunity({name: "r"});  /* poll renders the open viewer */
  const opened = tabEl.innerHTML.includes("print(1)");
  _wbInsp.scrollTop = 200;
  await _wbClick(rowBtn("r:858"));
  const moved = tabEl.innerHTML.includes("Plain question");
  await _wbClick(rowBtn("r:1"));
  await _wbSleep(60);
  const backViewer = tabEl.innerHTML.includes("print(1)");
  const backScroll = _wbInsp.scrollTop;
  _wbInsp.scrollTop = 175;
  LIST.items[0].updated_at = "2026-10-25T00:00:00+00:00";
  await paintRepoCommunity({name: "r"});  /* quiet poll: real path */
  console.log(JSON.stringify({
    opened: opened, moved: moved, backViewer: backViewer,
    backScroll: backScroll,
    pollViewer: tabEl.innerHTML.includes("print(1)"),
    pollScroll: _wbInsp.scrollTop,
    pollSel: discSelectedNum("r") === "1",
  }));
})();
""" % (json.dumps(wb_list(
            [wb_item(1, "Opus support", "reviewed"),
             wb_item(858, "Plain question", "unreviewed")])),
       json.dumps({"1": node1, "858": node858}),
       json.dumps(attempt_variant("r1", "review-1-r1.json",
                                  "print(1)\\n"))),
            prelude_extra=WORKBENCH_FAKE_JS)
        got = out[-1]
        self.assertTrue(got["opened"], got)
        self.assertTrue(got["moved"], got)
        self.assertTrue(got["backViewer"], got)
        self.assertEqual(got["backScroll"], 200, got)
        self.assertTrue(got["pollViewer"], got)
        self.assertEqual(got["pollScroll"], 175, got)
        self.assertTrue(got["pollSel"], got)

    def test_toggle_detail_keeps_saved_position_without_heading_scroll(self):
        # Rendered 1010->26 switch/return loss: the fake heading
        # scrollIntoView above reproduces the browser position reset.
        node1 = wb_node(1, "Opus support", "reviewed")
        node858 = wb_node(858, "Plain question", "unreviewed")
        out = run_driver("""
LIST = %s;
NODES = %s;
_wbNumberAware(NODES);
function rowBtn(key) {
  return tabEl.querySelectorAll("[data-discdetail]")
    .find((b) => b.dataset.discdetail === key);
}
(async () => {
  await paintRepoCommunity({name: "r"});
  await _wbClick(rowBtn("r:1"));
  const freshYank = _wbScrollTrail.includes("focus:1");
  _wbInsp.scrollTop = 150;
  await _wbClick(rowBtn("r:858"));
  _wbFocusTrail.length = 0;
  _wbScrollTrail.length = 0;
  await _wbClick(rowBtn("r:1"));
  await _wbSleep(60);
  console.log(JSON.stringify({
    freshYank: freshYank,
    restored: _wbInsp.scrollTop,
    noYank: !_wbScrollTrail.includes("focus:1"),
    focusMoved: _wbFocusTrail.includes("focus:1"),
    selBack: discSelectedNum("r") === "1",
  }));
})();
""" % (json.dumps(wb_list(
            [wb_item(1, "Opus support", "reviewed"),
             wb_item(858, "Plain question", "unreviewed")])),
       json.dumps({"1": node1, "858": node858})),
            prelude_extra=WORKBENCH_FAKE_JS)
        got = out[-1]
        self.assertTrue(got["freshYank"], got)
        self.assertEqual(got["restored"], 150, got)
        self.assertTrue(got["noYank"], got)
        self.assertTrue(got["focusMoved"], got)
        self.assertTrue(got["selBack"], got)

    def test_disclosure_open_and_close_survive_switch_and_poll(self):
        node1 = wb_node(1, "Opus support", "reviewed")
        node858 = wb_node(858, "Plain question", "unreviewed")
        outer_js = r"""
/* Small outer-reader stand-in (mobile #view): records every outer scroll
   restore so the test proves the final position lands AFTER disclosure. */
const _wbOuterTrail = [];
let _wbOuterTop = 0;
const _wbOuter = {};
/* Rendered-browser rule observed on the real page: a deep position requested
   while the long source is still collapsed clamps to the collapsed height;
   once expanded, the full position holds. */
Object.defineProperty(_wbOuter, "scrollTop", {
  get() { return _wbOuterTop; },
  set(v) {
    const card = tabEl.querySelector("[data-discdetailcard]");
    const f = card && card.querySelector(
      'details[data-dk="disc-att-r1-file-0"]');
    const open = !!(f && f.open);
    _wbOuterTop = open ? v : Math.min(v, 100);
    _wbOuterTrail.push({top: _wbOuterTop, fileOpen: open}); },
});
_wbOuter.scrollHeight = 10000; _wbOuter.clientHeight = 800;
tabEl.parentElement = _wbOuter;
function scrollerOf(el) {
  let n = el ? el.parentElement : null;
  while (n) {
    if (n.scrollHeight > n.clientHeight + 4) return n;
    n = n.parentElement;
  }
  return null;
}
captureReviewState = (box) => {
  const card = box.querySelector("[data-discdetailcard]");
  const details = card ? card.querySelectorAll("details[data-dk]") : [];
  return {key: null,
    open: details.filter((d) => d.open).map((d) => d.dataset.dk),
    top: _wbOuterTop, anchor: null};
};
restoreReviewStateAsync = async (box, st) => {
  if (!st) return;
  const sc = scrollerOf(box);
  if (sc && typeof st.top === "number") sc.scrollTop = st.top;
  (st.open || []).forEach((k) => {
    const d = box.querySelector('details[data-dk="' + k + '"]');
    if (d) d.open = true;
  });
};
const _wbRealNow = Date.now;
"""
        out = run_driver("""
LIST = %s;
NODES = %s;
ATTEMPT = %s;
_wbNumberAware(NODES);
function rowBtn(key) {
  return tabEl.querySelectorAll("[data-discdetail]")
    .find((b) => b.dataset.discdetail === key);
}
function cardDk(key) {
  const card = tabEl.querySelector("[data-discdetailcard]");
  return card && card.querySelector('details[data-dk="' + key + '"]');
}
(async () => {
  await paintRepoCommunity({name: "r"});
  await _wbClick(rowBtn("r:1"));
  await discLoadAttempt({name: "r"}, "r:1|review-1-r1.json");
  await paintRepoCommunity({name: "r"});  /* poll renders the viewer */
  cardDk("disc-att-r1-file-0").open = true;  /* user opens the source */
  cardDk("disc-r-1-body").open = false;      /* user closes the body */
  await _wbClick(rowBtn("r:858"));
  await _wbClick(rowBtn("r:1"));
  await _wbSleep(60);
  const fileKept = cardDk("disc-att-r1-file-0");
  const bodyKept = cardDk("disc-r-1-body");
  const excerptBack = tabEl.innerHTML.includes("print(1)");
  _wbOuter.scrollTop = 1295;  /* reader deep in the long file */
  _wbOuterTrail.length = 0;
  LIST.items[0].updated_at = "2026-10-26T00:00:00+00:00";  /* changed */
  Date.now = () => _wbRealNow() + 31000;  /* normal TTL refetch, no reset */
  await paintRepoCommunity({name: "r"});  /* quiet poll: real path */
  const outerAfterPoll = _wbOuterTop;
  const last = _wbOuterTrail[_wbOuterTrail.length - 1] || {};
  const filePolled = cardDk("disc-att-r1-file-0");
  await _wbClick(rowBtn("r:858"));
  const otherBody = cardDk("disc-r-858-body");
  console.log(JSON.stringify({
    fileOpen: !!(fileKept && fileKept.open),
    bodyClosed: !!(bodyKept && !bodyKept.open),
    excerpt: excerptBack,
    outerTop: outerAfterPoll,
    outerLastOpen: !!last.fileOpen,
    outerLastTop: last.top,
    filePolled: !!(filePolled && filePolled.open),
    otherDefault: !!(otherBody && otherBody.open),
    selBack: discSelectedNum("r") === "858",
  }));
})();
""" % (json.dumps(wb_list(
            [wb_item(1, "Opus support", "reviewed"),
             wb_item(858, "Plain question", "unreviewed")])),
       json.dumps({"1": node1, "858": node858}),
       json.dumps(attempt_variant("r1", "review-1-r1.json",
                                  "print(1)\\n"))),
            prelude_extra=WORKBENCH_FAKE_JS + outer_js)
        got = out[-1]
        self.assertTrue(got["fileOpen"], got)
        self.assertTrue(got["bodyClosed"], got)
        self.assertTrue(got["excerpt"], got)
        self.assertEqual(got["outerTop"], 1295, got)
        self.assertTrue(got["outerLastOpen"], got)
        self.assertEqual(got["outerLastTop"], 1295, got)
        self.assertTrue(got["filePolled"], got)
        self.assertTrue(got["otherDefault"], got)
        self.assertTrue(got["selBack"], got)

    def test_back_restores_queue_focus(self):
        node1 = wb_node(1, "Opus support", "reviewed")
        out = run_driver("""
LIST = %s;
NODE = %s;
(async () => {
  await paintRepoCommunity({name: "r"});
  await _wbClick(tabEl.querySelectorAll("[data-discdetail]")[0]);
  const back = tabEl.querySelectorAll("[data-discback]")[0];
  const hasBack = !!back;
  if (back) await _wbClick(back);
  await _wbSleep(120);  /* discBack restores focus in a paint .then */
  const html = tabEl.innerHTML;
  console.log(JSON.stringify({
    hasBack: hasBack,
    selCleared: discSelectedKey("r") === null,
    rows: (html.match(/data-discrow="/g) || []).length,
    focused: _wbFocusTrail.includes("row:r:1"),
    scrolled: _wbScrollTrail.includes("row:r:1"),
  }));
})();
""" % (json.dumps(wb_list(
            [wb_item(1, "Opus support", "reviewed"),
             wb_item(858, "Plain question", "unreviewed")])),
       json.dumps(node1)),
            prelude_extra=WORKBENCH_FAKE_JS)
        got = out[-1]
        self.assertTrue(got["hasBack"], got)
        self.assertTrue(got["selCleared"], got)
        self.assertEqual(got["rows"], 2, got)
        self.assertTrue(got["focused"], got)
        self.assertTrue(got["scrolled"], got)

    def test_long_list_position_and_back(self):
        items = [wb_item(n, "Long item %d with a deliberately long title "
                           "for queue wrapping" % n, "unreviewed")
                 for n in range(1, 31)]
        nodes = {str(n): wb_node(n, items[n - 1]["title"], "unreviewed")
                 for n in range(1, 31)}
        out = run_driver("""
LIST = %s;
NODES = %s;
_wbNumberAware(NODES);
(async () => {
  await paintRepoCommunity({name: "r"});
  const rows = tabEl.querySelectorAll("[data-discdetail]");
  await _wbClick(rows[rows.length - 1]);
  const selLast = discSelectedNum("r");
  _wbInsp.scrollTop = 250;
  LIST.items[29].updated_at = "2026-10-25T00:00:00+00:00";
  await paintRepoCommunity({name: "r"});  /* quiet poll: real path */
  const pollScroll = _wbInsp.scrollTop;
  const pollTitle = tabEl.innerHTML.includes("Long item 30");
  await _wbClick(tabEl.querySelectorAll("[data-discback]")[0]);
  await _wbSleep(120);  /* discBack restores focus in a paint .then */
  console.log(JSON.stringify({
    rows: rows.length, selLast: selLast,
    pollScroll: pollScroll, pollTitle: pollTitle,
    selCleared: discSelectedKey("r") === null,
    focused: _wbFocusTrail.includes("row:r:30"),
  }));
})();
""" % (json.dumps(wb_list(items)), json.dumps(nodes)),
            prelude_extra=WORKBENCH_FAKE_JS)
        got = out[-1]
        self.assertEqual(got["rows"], 30, got)
        self.assertEqual(got["selLast"], "30", got)
        self.assertEqual(got["pollScroll"], 250, got)
        self.assertTrue(got["pollTitle"], got)
        self.assertTrue(got["selCleared"], got)
        self.assertTrue(got["focused"], got)

    def test_inspector_arrows_plain_navigate_guarded_otherwise(self):
        node1 = wb_node(1, "Opus support", "reviewed")
        node858 = wb_node(858, "Plain question", "unreviewed")
        out = run_driver("""
LIST = %s;
NODES = %s;
_wbNumberAware(NODES);
let prevented = 0;
function key(k, target, extra) {
  return Object.assign({key: k, target: target,
    preventDefault: function() { prevented++; }}, extra || {});
}
(async () => {
  await paintRepoCommunity({name: "r"});
  const rows = tabEl.querySelectorAll("[data-discdetail]");
  await _wbClick(rows[0]);
  const insp = tabEl.querySelector(".winspect");
  insp.onkeydown(key("ArrowRight", {tagName: "DIV"}));
  await _wbSleep(120);
  const movedRight = discSelectedNum("r");
  insp.onkeydown(key("ArrowLeft", {tagName: "DIV"}));
  await _wbSleep(120);
  const movedLeft = discSelectedNum("r");
  const beforeMod = discSelectedNum("r");
  insp.onkeydown(key("ArrowRight", {tagName: "DIV"}, {ctrlKey: true}));
  await _wbSleep(60);
  insp.onkeydown(key("ArrowRight", {tagName: "TEXTAREA"}));
  await _wbSleep(60);
  insp.onkeydown(key("ArrowLeft", {tagName: "CODE",
    closest: function(s) { return s === "pre" ? {} : null; }}));
  await _wbSleep(60);
  console.log(JSON.stringify({
    movedRight: movedRight, movedLeft: movedLeft,
    modKept: discSelectedNum("r") === beforeMod,
    prevented: prevented,
  }));
})();
""" % (json.dumps(wb_list(
            [wb_item(1, "Opus support", "reviewed"),
             wb_item(858, "Plain question", "unreviewed")])),
       json.dumps({"1": node1, "858": node858})),
            prelude_extra=WORKBENCH_FAKE_JS)
        got = out[-1]
        self.assertEqual(got["movedRight"], "858", got)
        self.assertEqual(got["movedLeft"], "1", got)
        self.assertTrue(got["modKept"], got)
        # Exactly the two plain navigations preventDefaulted.
        self.assertEqual(got["prevented"], 2, got)

    def test_semantic_ink_contrast_on_real_surfaces(self):
        import re
        src = Path(__file__).resolve().parent.parent / "src" / "openmainty" / "dashboard.html"
        css = src.read_text(encoding="utf-8")

        def lum(hexcolor):
            hexcolor = hexcolor.lstrip("#")
            rgb = [int(hexcolor[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]

            def lin(c):
                return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
            return (0.2126 * lin(rgb[0]) + 0.7152 * lin(rgb[1])
                    + 0.0722 * lin(rgb[2]))

        def contrast(fg, bg):
            hi, lo = sorted([lum(fg), lum(bg)], reverse=True)
            return (hi + 0.05) / (lo + 0.05)

        # Light default lives in :root; dark opt-in lives in
        # html[data-theme="dark"] (no OS media gate by design).
        root = re.search(r":root\s*\{(.*?)\}", css, re.S).group(1)
        dark = re.search(r"html\[data-theme=\"dark\"\]\s*\{(.*?)\}",
                         css, re.S).group(1)
        inks = dict(re.findall(r"--(ok|warn|bad|run)-ink:\s*(#[0-9a-fA-F]{6})",
                               root))
        self.assertEqual(set(inks), {"ok", "warn", "bad", "run"}, inks)
        # Dark theme keeps the base palette: inks alias the base tokens.
        for name in ("ok", "warn", "bad", "run"):
            self.assertIn("--%s-ink: var(--%s)" % (name, name), dark)
        # Selected-row surface: light accent at 0x14/255 over white.
        accent = re.search(r"--accent:\s*(#[0-9a-fA-F]{6})", root).group(1)
        ar, ag, ab = (int(accent[i:i + 2], 16) for i in (1, 3, 5))
        alpha = 0x14 / 255.0
        sel = "#%02x%02x%02x" % (round(ar * alpha + 255 * (1 - alpha)),
                                 round(ag * alpha + 255 * (1 - alpha)),
                                 round(ab * alpha + 255 * (1 - alpha)))
        for name, ink in sorted(inks.items()):
            for surface in ("#ffffff", "#f4f6fa", sel):
                self.assertGreaterEqual(
                    contrast(ink, surface), 4.5,
                    "%s %s on %s" % (name, ink, surface))
        # Primary-button text and body text clear 4.5 on their roles.
        accent_ink = re.search(r"--accent-ink:\s*(#[0-9a-fA-F]{6})",
                               root).group(1)
        self.assertGreaterEqual(contrast(accent_ink, accent), 4.5,
                                "accent-ink on accent")
        ink = re.search(r"--ink:\s*(#[0-9a-fA-F]{6})", root).group(1)
        panel = re.search(r"--panel:\s*(#[0-9a-fA-F]{6})", root).group(1)
        self.assertGreaterEqual(contrast(ink, panel), 4.5,
                                "body ink on panel")
        for raw in ["color: var(--ok)", "color: var(--warn)",
                    "color: var(--bad)", "color: var(--run)",
                    "color:var(--ok)", "color:var(--warn)",
                    "color:var(--bad)", "color:var(--run)"]:
            self.assertNotIn(raw, css)
